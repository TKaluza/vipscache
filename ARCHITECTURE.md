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

Original files are identified by `file_id = xxh3-128(content)` and stored under `raw/<file_id>`. Client and worker roots may be different absolute paths, but both use the same relative storage layout. Stores created with the earlier 64-bit IDs are not migrated; discard `raw/` and `cache/` contents and re-ingest originals when moving to this format.

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
    ac045e19e0574d13f0c2b9a84d9e6721
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
      "file_id": "ac045e19e0574d13f0c2b9a84d9e6721",
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

A duplicate `materialize` for a leaf that is already being rendered waits at most
`IMGCACHE_BUSY_TIMEOUT_SECONDS` for the in-flight render instead of blocking a pool
thread for the whole render, then receives a busy reply:

```json
{
  "ok": false,
  "error": {"type": "Busy", "message": "render for this key is already in progress"},
  "retry_after": 2.0
}
```

Busy is an internal IPC property, not API surface: a busy reply is a regular REP
response, so the client keeps its REQ socket and transparently resends the request
after `retry_after`. Busy retries and the timeout/socket-rebuild retry path share
one overall request deadline computed when the request starts. Callers never
observe a busy response; they either get the result or a timeout.

The worker also answers an `identify` request. It carries an `ImageSpec` (encode
optional) and returns libvips-derived metadata for that exact pipeline:

```json
{
  "method": "identify",
  "spec": {
    "source": {"file_id": "ac045e19e0574d13f0c2b9a84d9e6721", "mime": "application/pdf"},
    "operations": [{"name": "render", "params": {"page": 1, "dpi": 75}}]
  }
}
```

```json
{
  "ok": true,
  "meta": {
    "width": 620,
    "height": 876,
    "bands": 3,
    "interpretation": "srgb",
    "mode": "RGB",
    "has_alpha": false,
    "dpi": [75.0, 75.0],
    "n_pages": 1
  }
}
```

`identify` is served by `RenderWorker.measure(spec)`, which runs the same render
path as `materialize` (load the deepest `.v` parent, then apply operations forward)
but stops before encoding and reads header fields instead of writing a leaf. Because
it reuses the materialize path, the reported geometry is guaranteed to equal the
geometry of the leaf the same spec would produce. This makes libvips the single
authority for pixel geometry — including PDF `/Rotate` axis swaps and EXIF
orientation — so consumers never derive crop bounds from a separate source such as
pypdf. `measure` does not write `.v` nodes or mutate the cache tree as a side effect; it is a read-only probe. A worker may memoize the resulting metadata in its local state store, but that store is advisory and must never influence which pixels a key produces.

An `ImgCacheClient` may also keep a bounded in-process metadata memo keyed by the
pipeline parent key. This only skips duplicate `identify` roundtrips for identical
pipelines; it does not affect rendering identity or cache paths.

The client uses REQ semantics with a client-side retry pattern:

- Clients poll for replies with a bounded timeout.
- On timeout or ZMQ socket error, the client closes and recreates the REQ socket before retrying.
- Sockets are not shared across threads. A long-lived `ImgCacheClient` may be shared, but its sync ZMQ sockets are lazy per thread or per session.
- Sockets use `LINGER=0` so shutdown does not hang on unsent messages.
- Default timeout is intentionally long because image/PDF work can be CPU-heavy.
- Async materialization uses an async ZMQ path; callers reach it through `await image`, `image.apath()`, `image.aread_bytes()`, or `client.aget(spec)`.

For local single-host use, prefer `ipc://<runtime-dir>/imgcache-worker.sock`. For Docker container-to-container use, prefer `tcp://worker:<port>` where `worker` is the Compose/service DNS name; do not use `localhost` unless client and worker are inside the same network namespace.

The worker runs a single REP socket when `max_workers=1`. For `max_workers>1`, it uses a ROUTER/DEALER broker with REP worker threads so multiple render jobs can be active in one worker process without changing the core `RenderWorker.materialize(spec)` implementation.

## Worker State

Workers can optionally keep a local LMDB state store enabled by `IMGCACHE_STATE_DIR`. This directory is worker-local mmap state, not shared cache data: do not place it under the shared imgcache root and do not mount it from a network filesystem.

The state store records:

- `identify` metadata keyed by the pipeline parent key, before encode, so encode variants can share metadata.
- request/build/materialization counters and timestamps.
- DAG edges between source/node keys for later analysis.

The renderer must work correctly with no state store or a broken state store. LMDB errors are isolated to the state layer, logged, and treated as cache misses or no-ops. The only durable source of truth for hits remains the filesystem/object store: opened files, `.v` nodes, leaves, and raw originals.

The worker wipes and recreates the LMDB environment when its stored version stamp changes. The stamp includes the render engine version, the state schema version, and the libvips version. This is deliberately not a migration layer.

`identify` writes metadata to LMDB but still does not write `.v` nodes or mutate the cache tree. The ZMQ worker exposes a read-only internal `stats` method for this state:

```json
{"method": "stats", "top": 10}
```

```json
{"method": "stats", "key": "node-or-source-key"}
```

The recorded usage stats and DAG edges are groundwork for future materialization
heuristics, such as automatically promoting frequently rebuilt intermediate nodes
to `.v` files. Adopting such a heuristic would revise the documented
no-auto-materialization policy and must be made as an explicit architecture
decision, not introduced as drift.

## Worker Configuration

Worker runtime settings are loaded with `pydantic-settings` from environment variables using the `IMGCACHE_` prefix.

Defaults:

```text
IMGCACHE_ENDPOINT=tcp://*:5555
IMGCACHE_ROOT=/data
IMGCACHE_MAX_WORKERS=4
IMGCACHE_TTL_SECONDS=604800
IMGCACHE_STATE_DIR=
IMGCACHE_STATE_MAP_SIZE_MB=1024
IMGCACHE_BUSY_TIMEOUT_SECONDS=2.0
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

`ImgCacheClient.register(path)` computes `xxh3-128(content)`, copies the source file atomically into `raw/<file_id>` if needed, and returns a `CachedImage` whose `ImageSpec.source` references the original by `file_id`. `SourceSpec` does not carry an absolute original path. The worker resolves the original path from its own configured root.

Clients may memoize file hashes in-process by `(resolved_path, st_mtime_ns,
st_size)`. On a memo hit, ingest checks whether `raw/<file_id>` already exists
instead of re-reading and re-hashing the source bytes. Any stat change forces a
fresh content hash.

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
  Canonical JSON hashing and xxh3-128 file IDs.

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

src/imgcache/state.py
  Optional worker-local LMDB state for metadata, usage stats, and DAG edges.

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

tests/test_state.py
  Worker-local LMDB state tests: metadata memoization, version wipe, stats,
  DAG edges, and state-failure isolation.

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

## v2 Direction (Exploratory)

The following is a recorded design exploration, not yet implemented behavior. It
captures how the operation surface, the source model, and the builder could grow
without breaking the content-addressed cache. The guiding invariant is unchanged:
the cache layer stays fully concrete, and every cached node remains a closed,
deterministic function of its inputs.

### Operation Surface Derived From Introspection

Rather than hand-curating a wrapper per libvips operation, the set of allowed
pipeline operations can be derived mechanically from `pyvips.Introspect.get(name)`.
For each operation the introspection exposes `member_x` (the primary image input),
`required_input`/`required_output`, `optional_input`, and per-argument GTypes via
`details[arg]["type"]` (comparable against `pyvips.GValue.image_type`).

That data classifies every operation automatically:

```text
pipeline op   member_x set, required output is an image  -> allowed in a chain
analysis op   output GType is not an image (e.g. double) -> belongs to identify/measure, not materialize
generator     member_x is None                           -> a source, not a chain step (see below)
multi-input   two or more image args in required_input   -> a merge / DAG node (see below)
load/save     *load, *save                               -> already covered by source decode + encode
```

The worker builds a frozen manifest from this classification (allowed nicknames,
per-argument GType for canonicalization, defaults). The manifest is data, not code,
so the client can validate specs against it without importing pyvips, preserving the
client/worker split. `engine_version` must include the libvips version, since the
manifest and operation semantics belong to a specific libvips release.

The `Operation(name, params)` spec stays generic and dispatches through a single
`pyvips.Operation.call(name, image, **params)` path. Operation order is part of
identity and is never normalized: `crop -> resize` and `resize -> crop` produce
different pixels and must stay different keys. The canonical operation order in
"Pipeline Shape" remains a documentation recommendation, not a normalizer.

Args that are not JSON-serializable (image-valued args, blobs, convolution masks,
ICC profiles as files) are excluded from the generic param path. Blob/array-valued
args, if added later, must be content-hashed into the key.

### Generators As A Source Variant

A generator (`black`, `text`, `gaussnoise`) produces a root image from parameters
instead of from file content. This is structurally identical to a source decode, so
`source` becomes a tagged union:

```text
FileSource(file_id, mime)        # identity = content hash (today)
GeneratedSource(name, params)    # identity = hash(name + params + engine_version)
```

Both are roots without a parent, both deterministic, both addressable. Two
reproducibility hazards must be guarded:

- `gaussnoise`/`perlin`/`worley` are only cacheable with a fixed `seed` in params.
  Without it, the same spec yields different pixels and the key is invalid.
- `text` depends on the worker's font environment, which is not captured by a
  content hash. Either exclude `text` or fold the font configuration into
  `engine_version`; otherwise two workers with different fonts share a key but
  produce different pixels.

### Merges As Multi-Parent DAG Nodes

A merge (`composite2`, `join`, `bandjoin`) is image-in -> image-out, so it is a
node, not an encode. Encode is terminal (image -> bytes); a merge produces a new
image `c` that can still be resized, cropped, and only then encoded. Modeling a
merge as an encode would wrongly forbid further operations on the result.

The only thing that changes versus a linear chain is the arity of the inputs. The
relation `a + b = c` is plain content-addressing over multiple input keys:

```text
chain node:   node_key = parent_key + op + params + engine_version
merge node:   node_key = hash( (key_a, key_b) + op + params + engine_version )
```

Inputs are ordered, not a set: for `composite2` the `base` and `overlay` arguments
are not interchangeable, so `a + b` and `b + a` are different keys.

The spec grows additively rather than becoming N-ary everywhere. The primary
lineage (`member_x`) stays the main parent; the remaining image arguments are
referenced as additional input specs:

```python
Operation(
    name="composite2",
    params={"mode": "over"},
    inputs=(overlay_spec,),   # extra ImageSpec inputs beyond the primary lineage
)
```

This keeps the linear common case simple and only merge nodes carry extra inputs.
The one place that changes in "Request Flow" is the backward scan: instead of
walking a single chain back to the deepest `.v` parent, the worker resolves each
input subtree recursively through the same materialize path, then applies the
merge. A `.v` parent of an overlay is a cache hit exactly like one on the primary
lineage. This is the deliberate point where the model becomes a DAG rather than a
tree, and it should be adopted as an explicit decision, not introduced incidentally.

### Parametric Builder Layer With Late Binding

Fixed input specs are too rigid once the requested output size varies. When output
width changes, the sensible input sizes change with it: an overlay should be resized
to the target resolution before compositing, not upscaled afterward. The input
parameters therefore depend on the output request.

This must not leak into the cache layer. A node key is only a valid content address
when all params are closed; "resize to whatever the output wants" inside a cached
node would break determinism. The resolution is two layers, and the lower one
already exists:

```text
Template / Builder (CachedImage)   parametric: "merge overlay onto base, both at
                                    target width W, overlay at 20% of W"
Cache (ImageSpec DAG)              concrete: resize(base, 800), resize(overlay, 160),
                                    composite2(...) -- every param closed
```

This is the existing split where `CachedImage` is the builder and no rendering
happens while the pipeline is described. The missing piece is that the builder may
carry late-bound parameters (the target size) that fan out across several inputs.
Calling `.width(800)` expands the template into a fully concrete DAG, and only that
concrete DAG crosses the worker boundary. Distinct sizes produce distinct concrete
specs and distinct keys; a resized overlay node is reused across every output that
needs that exact size.

When an input is defined relative to another (overlay = 20% of base width) and the
base width is only known after rendering (e.g. the base is a PDF page render),
expansion is a small fixpoint: `identify` the base, compute the dependent params,
then freeze the concrete DAG. This reuses the existing read-only `measure`/`identify`
path and writes no nodes. So input specs are not fixed values but functions of the
output parameters that the builder resolves to concrete specs before any worker
interaction.

### Object Store Direction And The ZMQ Data Plane

The POSIX root may later be complemented by an object store backend (S3 via
obstore). The seam for this already exists: `io.py` is the only place that
touches the filesystem for cache content, and the layout derives POSIX-relative
paths everywhere. An object store backend is a small store interface at that
seam — `read`, `write_atomic`, `exists`, `delete` — with the POSIX
implementation being today's behavior and an obstore implementation added
alongside, not instead.

The mapping is nearly one-to-one:

- Relative cache paths become object keys unchanged.
- "open = cache hit" becomes "GET = hit"; a 404 is the miss that triggers
  `materialize` over ZMQ, then a second GET.
- S3 PUT is atomic per object, so the temp-file + `os.replace` dance is simply
  unnecessary there; atomic publication is preserved by the store semantics.
- TTL eviction maps to native object lifecycle rules; `pinned/` and `raw/`
  prefixes are excluded from expiry, mirroring today's policy.

Placement is deliberate: `raw/` originals and encoded leaves belong in the
shared store, because they are what clients and delivery need. `.v` nodes stay
on worker-local disk — they are large, uncompressed, worker-internal reuse
artifacts, and uploading them would spend bandwidth on files no client ever
reads. The worker-local LMDB index earns its keep in this deployment shape: a
metadata or existence hit answers without any HEAD/GET roundtrip to the store.

One option is explicitly rejected: **image bytes are never streamed over
ZMQ**, neither now nor for the object store future. ZMQ remains a control
plane carrying specs, keys, and relative paths only. Streaming pixel data
through the worker would route every cache hit through it, turning a render
service into a data-plane bottleneck and coupling hit availability to worker
availability — today a cache hit is a direct open with no worker involvement,
and that property must survive any storage backend change. Clients always
fetch bytes from the storage layer (filesystem today, object store later),
never from the worker.

ZMQ payloads stay plain JSON. MessagePack (e.g. via msgspec) is an option if
payload size or parse time ever matters, but the payloads are small and JSON is
easier to inspect while the protocol is still moving.

### Worker Core In Rust (Speculative)

libvips performs the heavy pixel work in C, so Rust would not make transforms
meaningfully faster by itself. The candidate benefits are state management,
concurrency control, DAG planning, and a robust long-running daemon. If this is
pursued, LMDB stays the worker state store so Python and Rust can share the same
database directly (Rust side via a wrapper such as `heed`); `redb` would only be
considered if the worker became Rust-only and dropping the native LMDB dependency
outweighed LMDB's maturity.
