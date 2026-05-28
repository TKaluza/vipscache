# imgcache Architecture

`imgcache` is a content-addressed image store for originals and derived image/PDF-page outputs. It models every image request as an immutable `ImageSpec`, stores reusable intermediate nodes as native libvips `.v` files, and stores final delivery files as leaves such as `.webp`, `.png`, or `.jpg`.

The package is split for two runtime roles:

- Clients ingest originals, build immutable image specs, compute keys and paths, read cache hits directly, and do not depend on libvips or `pyvips`.
- Render workers own all rendering, transformations, and cache writes. Workers depend on libvips through `pyvips`.

The current package extras reflect that split:

```text
pip install imgcache[client]   # includes pyzmq, no pyvips dependency
pip install imgcache[worker]   # includes pyzmq + pyvips; system libvips must be installed
```

## Core Model

Original files are identified by `file_id = xxh3-64(content)` and stored under `raw/<file_id>`. Client and worker roots may be different absolute paths, but both use the same relative storage layout.

The central spec type is `ImageSpec`:

```python
ImageSpec(
    source=SourceSpec(file_id="...", mime="image/jpeg", metadata={}),
    operations=(Operation(...), ...),
    encode=EncodeSpec(...) | None,
)
```

`ImageSpec` covers all states:

- Original: no operations, no encode.
- Intermediate pipeline: operations, no encode.
- Materializable output: optional operations plus encode.

There is no second spec type for derivatives. A derivative is simply an `ImageSpec` with an `encode`.

Each non-leaf node key is derived from:

```text
parent_key + operation_name + canonical_params + engine_version
```

Leaf keys are derived from:

```text
parent_key + encode_format + encode_params + engine_version
```

This means operation order is part of identity. For example, `crop -> rotate` is a different image spec from `rotate -> crop`.

Materialization is explicit and deliberately small:

```text
never  # stream through this node; do not write a .v
force  # write this node as native libvips .v
pin    # write this node as .v under pinned/ and exclude from normal eviction later
```

There is no `auto` materialization policy. If a caller wants reuse, it should choose `force` or `pin` intentionally.

## Pipeline Shape

`CachedImage` is the user-facing immutable builder around `ImageSpec`. Every operation returns a new `CachedImage`; no rendering happens while the pipeline is being described.

The canonical operation order is:

```text
page / render
normalize / colorspace / alpha
fast_rotate / flip / flop
crop
scale / resize
rotate
encode
```

PDFs use an explicit `page(page=..., dpi=...)` operation because rendering a page is the source decode step. Image files do not need a page/render operation; they are loaded into libvips and then transformed.

An `ImageSpec` without operations and without encode resolves to the original file under `raw/<file_id>`. An `ImageSpec` with operations but without encode is not materializable and must fail with a clear error when a path, file, or bytes are requested. An `ImageSpec` with encode is materializable as a cache leaf. Encode without operations is allowed as a re-encode of the original.

## Storage Layout

The store is file-based and rooted. `raw/` contains content-addressed originals. `cache/` contains derived cache files and is sharded by the first two hex characters of the key:

```text
<root>/
  raw/
    ac045e19e0574d13
  cache/
    nodes/
      ab/
        abcdef....v
    pinned/
      cd/
        cdef12....v
    leaves/
      34/
        345678....webp
        345678....png
```

Paths are derived in POSIX-relative form and translated to native filesystem paths at the root boundary. The filesystem is the primary index: if a file can be opened, it is a cache hit.

## Request Flow

```text
Client opens an original or receives a CachedImage/ImageSpec
  |
  v
If spec is original-only: return root/raw/<file_id>
  |
  v
If spec has operations but no encode: raise a clear error
  |
  v
Client derives leaf path for encoded specs
  |
  +-- open succeeds: return file/path/stream
  |
  +-- FileNotFoundError: ask worker to materialize(spec)
        |
        v
      Worker scans dependency chain backward
        |
        +-- deepest .v parent exists: load it
        |
        +-- no .v parent exists:
              - PDF: render first page node with pdfload
              - Image: load original into libvips
        |
        v
      Worker builds missing nodes forward
        |
        +-- force/pin nodes are written atomically as .v
        |
        v
      Worker writes final leaf atomically
```

Clients write originals under `raw/` during ingest, but never write derived cache files. Workers are the only writers under `cache/nodes`, `cache/pinned`, and `cache/leaves`. `pyvips.Image` objects never cross process, container, or API boundaries; only specs, keys, paths, and files do.

## Worker Boundary

The first supported out-of-process boundary is ZeroMQ with JSON payloads. This is intentionally internal IPC, not a public REST API.

Deployment targets:

- Same local system or one Docker container: client and worker run as separate Python interpreters and communicate over `ipc://`.
- Docker deployment: client/app container and worker container mount the same imgcache root at their own local paths and communicate over internal `tcp://`.
- Worker container image: build with `podman build -f Containerfile.worker -t imgcache-worker:local .`.
- The example Compose file in `examples/compose.yaml` exposes the worker only to the internal Compose network.
- `scripts/stress_worker_memory.sh` runs a longer Podman memory stress test with a hard container memory limit and logs stats under `.stress-runs/`.

The ZMQ messages contain only specs and results. Image bytes stay on shared storage.

Request:

```json
{
  "method": "materialize",
  "spec": {
    "source": {
      "file_id": "ac045e19e0574d13",
      "mime": "image/jpeg",
      "metadata": {}
    },
    "operations": [],
    "encode": {
      "format": "webp",
      "params": {
        "quality": 82
      }
    }
  }
}
```

Success response:

```json
{
  "ok": true,
  "relpath": "cache/leaves/34/345678....webp"
}
```

The client derives the local result path from its own root. A worker may include a relative path for diagnostics, but clients must not rely on a worker-local absolute path.

Error response:

```json
{
  "ok": false,
  "error": {
    "type": "ValueError",
    "message": "dpi exceeds worker limit"
  }
}
```

The client uses REQ semantics with a client-side retry pattern:

- Clients poll for replies with a bounded timeout.
- On timeout or ZMQ socket error, the client closes and recreates the REQ socket before retrying.
- Sockets are not shared across threads. A long-lived `ImgCacheClient` may be shared, but its sync ZMQ sockets are lazy per thread or per session.
- Sockets use `LINGER=0` so shutdown does not hang on unsent messages.
- Default timeout is intentionally long because image/PDF work can be CPU-heavy.
- Async materialization uses an async ZMQ path; callers reach it through `await image`, `image.apath()`, `image.abytes()`, or `client.aget(spec)`.

For local single-host use, prefer `ipc://<runtime-dir>/imgcache-worker.sock`. For Docker container-to-container use, prefer `tcp://worker:<port>` where `worker` is the Compose/service DNS name; do not use `localhost` unless client and worker are inside the same network namespace.

The worker runs a single REP socket when `max_workers=1`. For `max_workers>1`, it uses a ROUTER/DEALER broker with REP worker threads so multiple render jobs can be active in one worker process without changing the core `RenderWorker.materialize(spec)` implementation.

## Worker Configuration

Worker runtime settings are loaded with `pydantic-settings` from environment variables using the `IMGCACHE_` prefix.

Defaults:

```text
IMGCACHE_ENDPOINT=tcp://*:5555
IMGCACHE_ROOT=/data
IMGCACHE_MAX_WORKERS=4
IMGCACHE_TTL_SECONDS=604800
IMGCACHE_LIBVIPS_CONCURRENCY=1
IMGCACHE_LIBVIPS_CACHE_MAX_MEM_MB=128
IMGCACHE_LIBVIPS_CACHE_MAX_FILES=100
IMGCACHE_LIBVIPS_CACHE_MAX_OPS=0
```

`IMGCACHE_LIBVIPS_CONCURRENCY=1` is intentional with `IMGCACHE_MAX_WORKERS=4`: it keeps one render job from expanding into many libvips threads and stealing CPU from other jobs. The worker applies libvips cache settings at process startup.

`IMGCACHE_LIBVIPS_CACHE_MAX_OPS=0` disables libvips' operation cache by default. `imgcache` already has its own content-addressed disk cache, and derivative workers typically process many different images, so keeping libvips operation results in RAM is not the default policy.

## Originals

Originals can be stored flat by content hash:

```text
<root>/raw/<file_id>
```

`ImgCacheClient.open(path)` computes `xxh3-64(content)`, copies the source file atomically into `raw/<file_id>` if needed, and returns a `CachedImage` whose `ImageSpec.source` references the original by `file_id`. `SourceSpec` does not carry an absolute original path. The worker resolves the original path from its own configured root.

## Eviction

TTL eviction is file-based and deletes old files under `cache/nodes/` and `cache/leaves/`. It does not delete `cache/pinned/` or `raw/`. The current policy uses file `mtime`.

The ZMQ worker exposes an internal `evict_ttl` request, and the underlying function is `imgcache.eviction.evict_ttl(layout, ttl_seconds)`.

## Repository Map

```text
pyproject.toml
  Project metadata, Python version, extras, and dev dependencies.

Containerfile.worker
  uv-based worker image. Installs system libvips and syncs imgcache[worker].

.containerignore
  Keeps local virtualenvs, tests, caches, and git data out of container builds.

examples/compose.yaml
  Compose-style worker deployment with a mounted imgcache root, healthcheck,
  and IMGCACHE_* defaults.

src/imgcache/__init__.py
  Public package exports.

src/imgcache/hash.py
  Canonical JSON hashing and xxh3-64 file IDs.

src/imgcache/spec.py
  Immutable source, image, operation, node, and encode specs. Also contains
  canonical operation ordering.

src/imgcache/layout.py
  Cache-relative path derivation for nodes, pinned nodes, and leaves.

src/imgcache/originals.py
  Content-addressed raw originals store under <root>/raw.

src/imgcache/eviction.py
  TTL eviction for nodes and leaves. Pinned nodes are kept.

src/imgcache/settings.py
  pydantic-settings worker configuration and libvips runtime cache setup.

src/imgcache/io.py
  Atomic write helper and cache-hit open helper.

src/imgcache/client.py
  Client API. It ingests originals, builds CachedImage objects, computes paths,
  opens hits, and delegates cache misses. This module must remain free of
  pyvips/libvips imports.

src/imgcache/zmq_client.py
  Client-side ZeroMQ adapter. It sends JSON materialize requests and returns paths.
  This module must remain free of pyvips/libvips imports.

src/imgcache/worker.py
  Render worker orchestration. It backtracks to the deepest materialized parent,
  builds forward, and writes nodes/leaves atomically.

src/imgcache/zmq_worker.py
  Worker-side ZeroMQ server. It receives JSON requests and calls
  RenderWorker.materialize(spec). With max_workers > 1 it uses a ROUTER/DEALER
  broker plus REP worker threads.

src/imgcache/executor.py
  Worker-side libvips execution adapter. This is where pyvips is imported lazily.

src/imgcache/limits.py
  Worker-side limits for formats, DPI, and pixel counts.

tests/test_specs.py
  ImageSpec hashing and canonical pipeline tests.

tests/test_layout.py
  Cache layout tests.

tests/test_worker.py
  Worker/client behavior tests using generated local image data.

tests/test_zmq.py
  End-to-end IPC test for ImgCacheClient -> ZMQ adapter -> ZmqWorkerServer.

tests/test_real_assets.py
  Integration-style tests with a real PDF and a real Wikimedia image.

scripts/test_worker_container.sh
  Podman smoke test. Builds the worker image, runs a ZMQ worker container with
  a mounted imgcache root, materializes an image derivative from the host
  client, and verifies the cache leaf exists.

scripts/stress_worker_memory.sh
  Longer memory stress test. Builds the worker image, runs it with Podman
  memory limits, drives mixed PDF/image derivative requests over ZMQ, samples
  `podman stats`, and writes run logs under `.stress-runs/`. Use
  `STRESS_MODE=warm-cache` for realistic cache warmup behavior or
  `STRESS_MODE=cold-derivatives` to delete each requested leaf/materialized
  node before requesting it.

image_page_derivative_engine_plan_v4.md
  Original architectural plan. Useful context, but ARCHITECTURE.md describes
  the architecture decisions.
```

## Design Constraints For Future Work

- Do not add pyvips imports to client-facing modules.
- Clients may write raw originals during ingest, but must not write derived cache files.
- Keep all result-affecting parameters in specs and keys.
- Preserve atomic publication with temp files and `os.replace`.
- Keep `.v` nodes reproducible and disposable unless pinned.
- Prefer file open/stat over a required database.
- If a metadata index is added, keep it worker-internal unless the architecture is deliberately revised.
