# imgcache Architecture

`imgcache` is a content-addressed derivative cache for images and PDF pages. It models every derivative as an immutable DAG node, stores reusable intermediate nodes as native libvips `.v` files, and stores final delivery files as leaves such as `.webp`, `.png`, or `.jpg`.

The package is split for two runtime roles:

- Thin clients compute keys and paths, read cache hits directly, and do not depend on libvips or `pyvips`.
- Render workers own all rendering, transformations, and cache writes. Workers depend on libvips through `pyvips`.

The current package extras reflect that split:

```text
pip install imgcache[client]   # includes pyzmq, no pyvips dependency
pip install imgcache[worker]   # includes pyzmq + pyvips; system libvips must be installed
```

## Core Model

Original files are identified by `file_id = xxh3-64(content)`. Derivatives are immutable chains of operations. Each non-leaf node key is derived from:

```text
parent_key + operation_name + canonical_params + engine_version
```

Leaf keys are derived from:

```text
parent_key + encode_format + encode_params + engine_version
```

This means operation order is part of identity. For example, `crop -> rotate` is a different derivative from `rotate -> crop`.

Materialization is explicit and deliberately small:

```text
never  # stream through this node; do not write a .v
force  # write this node as native libvips .v
pin    # write this node as .v under pinned/ and exclude from normal eviction later
```

There is no `auto` materialization in the current implementation. If a caller wants reuse, it should choose `force` or `pin` intentionally.

## Pipeline Shape

There are two builders:

- `DerivativeSpec.build(...)` preserves the caller-provided operation order. Use this when the caller is intentionally constructing an exact DAG.
- `DerivativeSpec.canonical(...)` enforces the project order:

```text
render
normalize / colorspace / alpha
fast_rotate / flip / flop
crop
scale / resize
rotate
encode
```

PDFs use explicit `render(page=..., dpi=...)` because rendering a page is the source decode step. Image files do not need a render node; canonical building silently skips `render` for non-PDF sources. Images are loaded into libvips and then transformed.

## Cache Layout

The cache is file-based and sharded by the first two hex characters of the key:

```text
<cache-root>/
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

Paths are derived in POSIX form and translated to native filesystem paths at the layout boundary. The filesystem is the primary index: if a file can be opened, it is a cache hit.

## Request Flow

```text
Client receives or builds DerivativeSpec
  |
  v
Client derives leaf path
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
              - PDF: render first render node with pdfload
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

Clients never write cache files. Workers are the only writers. `pyvips.Image` objects never cross process, container, or API boundaries; only specs, keys, paths, and files do.

## Worker Boundary

The first supported out-of-process boundary is ZeroMQ with JSON payloads. This is intentionally internal IPC, not a public REST API.

Deployment targets:

- Same local system or one Docker container: client and worker run as separate Python interpreters and communicate over `ipc://`.
- Docker deployment: client/app container and worker container share cache/original mounts and communicate over internal `tcp://`.
- Worker container image: build with `podman build -f Containerfile.worker -t imgcache-worker:local .`.
- The example Compose file in `examples/compose.yaml` exposes the worker only to the internal Compose network.

The ZMQ messages contain only specs and results. Image bytes stay on shared storage.

Request:

```json
{
  "method": "materialize",
  "spec": {
    "source": {},
    "nodes": [],
    "leaf": {}
  }
}
```

Success response:

```json
{
  "ok": true,
  "path": "/shared/cache/leaves/..."
}
```

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
- Sockets are not shared across threads.
- Sockets use `LINGER=0` so shutdown does not hang on unsent messages.
- Default timeout is intentionally long because image/PDF work can be CPU-heavy.

For local single-host use, prefer `ipc://<runtime-dir>/imgcache-worker.sock`. For Docker container-to-container use, prefer `tcp://worker:<port>` where `worker` is the Compose/service DNS name; do not use `localhost` unless client and worker are inside the same network namespace.

The worker runs a single REP socket when `max_workers=1`. For `max_workers>1`, it uses a ROUTER/DEALER broker with REP worker threads so multiple render jobs can be active in one worker process without changing the core `RenderWorker.materialize(spec)` implementation.

## Worker Configuration

Worker runtime settings are loaded with `pydantic-settings` from environment variables using the `IMGCACHE_` prefix.

Defaults:

```text
IMGCACHE_ENDPOINT=tcp://*:5555
IMGCACHE_CACHE_ROOT=/cache
IMGCACHE_ORIGINALS_ROOT=/originals
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
<originals-root>/<file_id>
```

`OriginalsStore.put(path)` computes `xxh3-64(content)`, copies the source file atomically into that flat path if needed, and returns a `SourceSpec` pointing at the stored original. In container deployments, clients and workers must agree on the mounted originals path visible to the worker, usually `/originals/<file_id>`.

## Eviction

TTL eviction is file-based and deletes old files under `nodes/` and `leaves/`. It does not delete `pinned/`. The current policy uses file `mtime`.

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
  Compose-style worker deployment with cache/original volumes, healthcheck, and
  IMGCACHE_* defaults.

src/imgcache/__init__.py
  Public package exports.

src/imgcache/hash.py
  Canonical JSON hashing and xxh3-64 file IDs.

src/imgcache/spec.py
  Immutable source, operation, node, leaf, and derivative specs.
  Also contains canonical operation ordering.

src/imgcache/layout.py
  Cache path derivation for nodes, pinned nodes, and leaves.

src/imgcache/originals.py
  Flat content-addressed originals store.

src/imgcache/eviction.py
  TTL eviction for nodes and leaves. Pinned nodes are kept.

src/imgcache/settings.py
  pydantic-settings worker configuration and libvips runtime cache setup.

src/imgcache/io.py
  Atomic write helper and cache-hit open helper.

src/imgcache/client.py
  Thin client API. It computes paths, opens hits, and optionally delegates misses.
  This module must remain free of pyvips/libvips imports.

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
  Spec hashing and canonical pipeline tests.

tests/test_layout.py
  Cache layout tests.

tests/test_worker.py
  Worker/client behavior tests using generated local image data.

tests/test_zmq.py
  End-to-end IPC test for ThinClient -> ZmqWorkerClient -> ZmqWorkerServer.

tests/test_real_assets.py
  Integration-style tests with a real PDF and a real Wikimedia image.

scripts/test_worker_container.sh
  Podman smoke test. Builds the worker image, runs a ZMQ worker container with
  mounted cache/original folders, materializes an image derivative from the host
  client, and verifies the cache leaf plus forced .v node exist.

image_page_derivative_engine_plan_v4.md
  Original architectural plan. Useful context, but ARCHITECTURE.md describes
  the current implementation decisions.
```

## Current Implementation Status

Implemented:

- Content-addressed specs and cache paths.
- `xxh3-64` file IDs and derivative keys.
- Thin client without pyvips dependency.
- ZMQ client adapter for miss delegation.
- Worker with lazy pyvips dependency.
- ZMQ worker server wrapping the render worker.
- Native libvips `.v` intermediate nodes.
- Explicit `never / force / pin` materialization policy.
- Env-driven worker settings with pydantic-settings.
- Libvips cache defaults configured at worker startup.
- max-workers default of 4 for the ZMQ worker pool.
- Flat content-addressed originals store.
- TTL eviction for nodes and leaves.
- Healthcheck command.
- PDF page rendering through libvips.
- Image loading and transformations through libvips.
- Atomic writes.
- In-process leaf-key lock map.
- Basic worker limits.
- Tests for specs, layout, worker behavior, real PDF, and real image input.
- End-to-end ZMQ IPC test.
- Worker Containerfile and Podman smoke test.
- Podman smoke test runs under a memory limit and renders the sample PDF.

Not implemented yet:

- Cross-process inflight dedupe.
- Worker-internal metadata index.
- `copy_memory()` multi-output optimization.
- CLI.
- Documentation for deployment and system libvips installation.

## Design Constraints For Future Work

- Do not add pyvips imports to client-facing modules.
- Do not let clients write cache files.
- Keep all result-affecting parameters in specs and keys.
- Preserve atomic publication with temp files and `os.replace`.
- Keep `.v` nodes reproducible and disposable unless pinned.
- Prefer file open/stat over a required database.
- If a metadata index is added, keep it worker-internal unless the architecture is deliberately revised.
