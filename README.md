# vipscache

Content-addressed cache for image and PDF-page derivatives with a lazy, immutable Python API.

Clients ingest originals, build specs, and read cache hits directly from disk. A render worker owns libvips and is asked over ZeroMQ to materialize cache misses. Image bytes never cross the wire; only specs, keys, and relative paths do.

## Layout

```text
<root>/
  raw/        originals, named by file_id = xxh3-128(content)
  cache/
    nodes/    reusable intermediate .v files
    pinned/   pinned .v files, excluded from TTL eviction
    leaves/   encoded outputs (.webp, .png, .jpg, ...)
```

Derived files are keyed from the source, the ordered operations, the encode parameters, and the engine version. Operation order is part of identity. The filesystem is the index: if a file opens, it is a hit.

## Install

```bash
pip install 'vipscache[client]'   # no pyvips dependency
pip install 'vipscache[worker]'   # pyvips + lmdb; system libvips required
```

Requires Python >= 3.12.

## Usage

```python
from vipscache import VipsCacheClient

client = VipsCacheClient.zmq(root="/shared/vipscache", endpoint="tcp://127.0.0.1:5555")

preview = (
    client.register("example.jpg", mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

path = preview.path()     # materializes on miss, returns a local Path
blob = preview.read_bytes()
```

Every transformation returns a new immutable `CachedImage`; nothing renders until a path or bytes are requested. Operations: `page`, `thumbnail`, `autorotate`, `normalize`, `scale`, `resize`, `crop`, `crop_fraction`, `rotate`, `fast_rotate`, `flip`, `flop`. Encodes: `webp`, `png`, `jpg`, `avif`, `tif`. A transformed image without an encode raises when materialized; an untransformed image resolves to the original under `raw/`.

### Linked originals and reusable previews

```python
source = await client.alink("/storage/library/photo.jpg", file_id=known_xxh3_128)
base = source.thumbnail(longest_edge=2560).materialize("force")
grid = base.scale(longest_edge=320).webp(quality=80)
await grid.aensure(retain="pin")  # confirms the force node and durable leaf pin
path = await grid.apath()         # direct hit, even without the source or worker
await grid.aunpin()  # leaves bytes in place, starts a fresh TTL
await client.adelete([grid])  # deletes just these leaves, including pins
await client.aunlink(source.source.file_id)  # raw entry only; derivatives survive
await client.acleanup_staging(older_than=24 * 60 * 60)  # explicit import housekeeping
```

`link`/`alink` default to hardlinks. Supplying a validated 32-character lowercase
`file_id` avoids hashing; `verify=True` checks it. `register`/`aregister` accept
the same options while copying. Inputs linked into the store must remain
immutable. PicOrgi/the caller owns reference counting for duplicate originals
and calls `unlink` only after the last required reference disappears.

Both clients stage all new imports under `raw/.staging-<uuid>/source`. Python's
`cleanup_staging(older_than=86400)` / `acleanup_staging` and Node's
`cleanupStaging({olderThan: 86400})` return the number of removed staging entries.
They recognize only this schema and historical Python/Node staging names; foreign
hidden entries and canonical originals are preserved. Age is measured on the
staging directory. Legacy direct links also use ctime, avoiding the old original's
mtime as evidence of an abandoned import. No symlink targets are traversed.
Cleanup can overlap imports. If staging is removed during an import, the importer
raises `VipsCacheError` with `type="StagingLost"`; the caller decides whether to retry.
There is no automatic import retry or staging cleanup.

Symlinks use `mode="symlink"`; optional `link_target` is an absolute path as seen
by the worker. Dangling links are replaced atomically. Hardlinks across filesystem
or bind-mount boundaries fail with `CrossDeviceLink`; there is no copy fallback.
For container ingest, mount the common parent of the library and raw store **once**
in the client, e.g. `/mnt/photos:/storage` with `/storage/library` and
`/storage/vipscache/raw`. The worker can mount only `/mnt/photos/vipscache:/data`
and, if needed, the library separately read-only. Two separate client bind mounts
can prevent hardlinking even when their underlying dataset is identical.

`thumbnail` must be the first image operation. It uses libvips shrink-on-load,
applies EXIF orientation by default (`autorotate=False` opts out), preserves aspect
ratio and allows up/downscaling. `longest_edge` is a square bounding box; a single
`width` or `height` fixes that axis; both together define a bounding rectangle.
Rounding is libvips' nearest pixel (minimum one pixel); the thumbnail path uses
libvips' default sRGB processing and Lanczos reduction. All selectable options
are keyed, including the explicit `autorotate=True` default. Existing `scale`
keeps its semantics: `width=32,height=32` stretches to 32x32, while a thumbnail of
a 64x48 source becomes 32x24. `autorotate()` explicitly orients a full-resolution
pipeline and clears the orientation tag; ordinary loading/`normalize` do not.

`materialize("force"|"pin"|"never")` changes the last operation's policy without I/O.
`ensure()`/`aensure()` explicitly confirm all requested nodes, including earlier
nodes skipped by a deeper cached parent. Promoting a node to a pin reuses its bytes.
`ensure(retain="pin")` additionally confirms durable leaf retention. Retention is
an argument of this request, never part of `ImageSpec`, encoder options or keys.
An ordinary `ensure()` creates an expiring leaf if needed and never removes a pin.

`path()`/`apath()`, `open()` and byte reads always try the leaf directly. On misses,
`materialize` writes requested intermediate nodes along the chosen render path
as in 0.4.2; only `ensure` guarantees all of them. The same spec remains readable
after raw unlink or intermediate expiry, without a worker. Pins are removed only
by explicit `unpin()`/`aunpin()`; `delete()`/`adelete()` also work on a `CachedImage`.
`client.ensure`, `client.unpin`, and `client.delete` accept specs or built images.

PDFs stay PDFs until a page is selected:

```python
page = client.register("doc.pdf", mime="application/pdf").page(1, dpi=144).png()
```

Async uses the same object model:

```python
image = await client.aregister("example.jpg", mime="image/jpeg")
preview = image.scale(longest_edge=1024).webp()
path = await preview            # same as await preview.apath()
data = await preview.aread_bytes()
```

### Client method names

| Action | TypeScript (Promise API) | Python sync / async |
| --- | --- | --- |
| Register source | `client.register(path)` | `client.register(path)` / `await client.aregister(path)` |
| Confirm nodes / pin a leaf | `client.ensure(spec, {retain: "pin"})` | `image.ensure(retain="pin")` / `await image.aensure(retain="pin")` |
| Clean old import staging | `client.cleanupStaging({olderThan: 86400})` | `client.cleanup_staging(older_than=86400)` / `await client.acleanup_staging(older_than=86400)` |
| Query metadata | `client.identify(spec)` | `image.identify()` / `await image.aidentify()` |
| Calculate cache location only | `client.resolve(spec)` | `client.resolve(spec)` / `await client.aresolve(spec)` |
| Render if needed and open file | `client.open(spec)` | `image.open()` / `await image.aopen()` |
| Render if needed and read all bytes | `client.readBytes(spec)` | `image.read_bytes()` / `await image.aread_bytes()` |

TypeScript calls in the table require `await`. Python registration returns a
`CachedImage` builder; TypeScript returns a `Source` to include in an explicit
spec. Python `resolve` returns a local `Path`, TypeScript returns
`{key, relpath, spec}`. Neither checks file existence or renders; Python
`aresolve` is an async convenience wrapper with no I/O.

The caller closes handles returned by `open` / `aopen`. Python `aopen` returns
a regular file object: use `with await image.aopen() as handle`, and remember
its subsequent reads are synchronous. Prefer `aread_bytes()` for async reads.

### Metadata

`CachedImage` exposes Pillow-like metadata (`.size`, `.width`, `.height`, `.mode`, `.info`, `.n_pages`). The worker computes it over the exact render pipeline the spec would materialize, so reported geometry always matches the produced file — libvips owns geometry, including PDF `/Rotate` and EXIF orientation. Fractional crops resolve against that geometry on the worker:

```python
top_half = page.crop_fraction(bottom=0.5)
```

## Worker

```bash
VIPSCACHE_ROOT=/shared/vipscache VIPSCACHE_ENDPOINT='tcp://*:5555' uv run vipscache-zmq-worker
```

Configuration via environment (defaults shown):

```text
VIPSCACHE_ENDPOINT=tcp://*:5555
VIPSCACHE_ROOT=/data
VIPSCACHE_MAX_WORKERS=4
VIPSCACHE_TTL_SECONDS=604800
VIPSCACHE_STATE_DIR=                      # unset = LMDB state disabled
VIPSCACHE_STATE_MAP_SIZE_MB=1024
VIPSCACHE_BUSY_TIMEOUT_SECONDS=2.0
VIPSCACHE_MANAGEMENT_DIR=                 # local durable retention directory; see below
VIPSCACHE_MANAGEMENT_SESSION_TIMEOUT_SECONDS=300
VIPSCACHE_GC_INTERVAL_SECONDS=60          # 0 disables scheduled cleanup
VIPSCACHE_MAX_CACHE_BYTES=                # unset = no byte budget
VIPSCACHE_MAX_INPUT_PIXELS=               # unset = no input limit
VIPSCACHE_MAX_OUTPUT_PIXELS=              # unset = no output limit
VIPSCACHE_MAX_DPI=                        # unset = no PDF DPI limit
VIPSCACHE_LIBVIPS_CONCURRENCY=1
VIPSCACHE_LIBVIPS_CACHE_MAX_MEM_MB=128
VIPSCACHE_LIBVIPS_CACHE_MAX_FILES=100
VIPSCACHE_LIBVIPS_CACHE_MAX_OPS=0
```

`VIPSCACHE_STATE_DIR` enables an advisory worker-local LMDB store for metadata memoization, usage stats, and DAG edges. It must be a worker-local directory, never the shared root or a network mount. The renderer works fully without it.

Retention management is opt-in: set `VIPSCACHE_MANAGEMENT_DIR` (or explicitly
supply a management client) to start or connect to one local owner. No directory
is inferred from `STATE_DIR`, XDG or the home directory. Without management, normal
rendering and `ensure()` remain available, including existing file-based node pins;
`ensure(retain="pin")` requires the owner. Its SQLite catalog is durable: keep the
configured directory on local persistent storage and back it up. Render processes
communicate through private local Unix sockets; only that owner opens SQLite.
Several processes under the same OS user can use the same management directory.
The image uses `/state/stats` for disposable LMDB; retention remains disabled by
default, including in the container. The Compose example explicitly enables it
at `/state/retention` and mounts a named persistent volume on `/state`.

`VIPSCACHE_MANAGEMENT_SESSION_TIMEOUT_SECONDS` bounds idle owner sessions. Set it
above the longest expected render without a management message; rendering does
not send heartbeats. The first process that starts the shared owner sets this
timeout for all attached workers. A lost session cannot publish its result.

Stop previous workers/evictors before upgrading a shared cache. The owner imports
existing files and their `mtime`, including pinned `.v` nodes, in a background
reconciliation before enabling GC. Requests can render while reconciliation runs;
recovery of journaled publication/deletion completes before requests are accepted.
Missing TTL files are removed from the catalog; missing pins retain their intent.
Unchanged artifact rows are not rewritten. Changes commit in batches of at most
128 entries, while the filesystem scan still runs on every owner restart.
`unpin` also clears a pin when its file is missing.
It records its identity in `cache/.managed.json`; a missing or mismatched database
then blocks management and cleanup, while clients can still read ordinary hits.
Restore the matching catalog if lost; never delete its marker to bypass this check.
Use a stopped-owner backup of the entire management directory (including SQLite
WAL files), or SQLite's online backup API. Do not place SQLite on NFS/SMB.

TTL starts at publication, or at explicit unpin; reading does not extend it yet.
Periodic/manual cleanup skips pins and `raw/`. The optional byte budget covers
unretained nodes and leaves, oldest publication first, and is a soft target during
concurrent rendering. Direct `evict_ttl(layout, ...)` is only for unmanaged legacy
caches; use `ZmqWorkerClient.evict_ttl()` for managed caches.
Configuring `max_cache_bytes` without a management directory or injected management
client raises a startup error, including when the budget is zero.
For embedded use, pass `ManagementService(layout, local_dir).client` to
`RenderWorker(..., management=...)` and keep the service open for the worker lifetime.
Inactivity TTL and workers on multiple hosts remain planned extensions.

Python worker errors are `VipsCacheError` with `.type`: `OriginalMissing`,
`UnreadableFormat`, `LimitExceeded`, `InvalidSpec`, `Timeout`, and management/transport
categories, plus `StagingLost` during ingest. Known types use the `ErrorType`
string enum, and errors can be pickled. Unknown worker exceptions are `WorkerError`;
only errors with known meaning are mapped to input/format/source failures.
Timeout errors also inherit `TimeoutError`. Cancellation closes the
async request socket; later requests remain usable. A cancelled request may still
complete on the worker. Existing workers reject the new `ensure`/`unpin`/`delete`
methods rather than silently confirming unsupported retention.

Healthcheck:

```bash
uv run vipscache-zmq-healthcheck --endpoint tcp://127.0.0.1:5555
```

## Container

```bash
podman build --format docker -f Containerfile.worker -t vipscache-worker:local .
podman run --rm -p 127.0.0.1:5555:5555 \
  -e VIPSCACHE_ROOT=/data -e VIPSCACHE_ENDPOINT='tcp://*:5555' \
  -v "$PWD/.local/vipscache:/data:Z" \
  -v "$PWD/.local/vipscache-state:/state:Z" \
  vipscache-worker:local
```

See [examples/compose.yaml](examples/compose.yaml) for a Compose-style deployment with a dedicated state volume.

The worker builds libvips **8.18.7** from a SHA-256-verified source archive on
Debian trixie, with libheif (HEVC/AV1), JPEG XL, TIFF, PNG, WebP, JPEG, LCMS and
Poppler. Build configuration and installed codec versions are recorded in
`/opt/vips/share/build/`. Existing keys remain `vipscache-v1`; library micro/security
updates never change them automatically. A minor-line upgrade requires render
compatibility review. Recomputed bytes can differ across library/codec updates.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
uv run ty check --error-on-warning
scripts/test_worker_container.sh                                    # podman smoke test
STRESS_DURATION_SECONDS=600 scripts/stress_worker_memory.sh         # memory stress test
```

## Notes

- Clients never write under `cache/`; workers are the only cache writers.
- TTL and byte-budget eviction remove only unretained nodes/leaves, never pins or `raw/`.
- Duplicate concurrent renders of the same key are coalesced; the ZMQ client handles the internal busy/retry protocol transparently.
- PDF page numbers are one-based.
- Design details and invariants live in [ARCHITECTURE.md](ARCHITECTURE.md).

## TypeScript / Node client

[`clients/node`](clients/node/README.md) provides a small importable server-side
client over the existing ZeroMQ protocol and shared storage: register sources,
identify metadata, request derivatives and open cached files. Both languages
calculate XXH3-128 keys locally against the same
[contract and fixed test vectors](contracts/README.md).

Run `npm test --prefix clients/node` after `npm ci --prefix clients/node`.
`scripts/test_node_container.sh` checks native Node dependencies and real
rendering against the Python worker in separate containers.
