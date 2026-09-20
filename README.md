# imgcache

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
pip install 'imgcache[client]'   # no pyvips dependency
pip install 'imgcache[worker]'   # pyvips + lmdb; system libvips required
```

Requires Python >= 3.12.

## Usage

```python
from imgcache import ImgCacheClient

client = ImgCacheClient.zmq(root="/shared/imgcache", endpoint="tcp://127.0.0.1:5555")

preview = (
    client.register("example.jpg", mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

path = preview.path()     # materializes on miss, returns a local Path
blob = preview.read_bytes()
```

Every transformation returns a new immutable `CachedImage`; nothing renders until a path or bytes are requested. Operations: `page`, `normalize`, `scale`, `resize`, `crop`, `crop_fraction`, `rotate`, `fast_rotate`, `flip`, `flop`. Encodes: `webp`, `png`, `jpg`, `avif`, `tif`. A transformed image without an encode raises when materialized; an untransformed image resolves to the original under `raw/`.

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

### Client method names (0.3.0)

| Action | TypeScript (Promise API) | Python sync / async |
| --- | --- | --- |
| Register source | `client.register(path)` | `client.register(path)` / `await client.aregister(path)` |
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

For compatibility, Python `client.open` / `client.aopen` still **register
sources** and are aliases for `register` / `aregister`; they do not accept an
image spec or open a file handle. Existing `path_for`, `image.bytes`,
`image.abytes` and `image.ainfo` remain aliases for `resolve`, `read_bytes`,
`aread_bytes` and `aidentify`. New code should use the names in the table.
The new TypeScript package exposes `readBytes`, without a legacy `bytes` alias.

### Metadata

`CachedImage` exposes Pillow-like metadata (`.size`, `.width`, `.height`, `.mode`, `.info`, `.n_pages`). The worker computes it over the exact render pipeline the spec would materialize, so reported geometry always matches the produced file — libvips owns geometry, including PDF `/Rotate` and EXIF orientation. Fractional crops resolve against that geometry on the worker:

```python
top_half = page.crop_fraction(bottom=0.5)
```

## Worker

```bash
IMGCACHE_ROOT=/shared/imgcache IMGCACHE_ENDPOINT='tcp://*:5555' uv run imgcache-zmq-worker
```

Configuration via environment (defaults shown):

```text
IMGCACHE_ENDPOINT=tcp://*:5555
IMGCACHE_ROOT=/data
IMGCACHE_MAX_WORKERS=4
IMGCACHE_TTL_SECONDS=604800
IMGCACHE_STATE_DIR=                      # unset = LMDB state disabled
IMGCACHE_STATE_MAP_SIZE_MB=1024
IMGCACHE_BUSY_TIMEOUT_SECONDS=2.0
IMGCACHE_LIBVIPS_CONCURRENCY=1
IMGCACHE_LIBVIPS_CACHE_MAX_MEM_MB=128
IMGCACHE_LIBVIPS_CACHE_MAX_FILES=100
IMGCACHE_LIBVIPS_CACHE_MAX_OPS=0
```

`IMGCACHE_STATE_DIR` enables an advisory worker-local LMDB store for metadata memoization, usage stats, and DAG edges. It must be a worker-local directory, never the shared root or a network mount. The renderer works fully without it.

Healthcheck:

```bash
uv run imgcache-zmq-healthcheck --endpoint tcp://127.0.0.1:5555
```

## Container

```bash
podman build --format docker -f Containerfile.worker -t imgcache-worker:local .
podman run --rm -p 127.0.0.1:5555:5555 \
  -e IMGCACHE_ROOT=/data -e IMGCACHE_ENDPOINT='tcp://*:5555' \
  -v "$PWD/.local/imgcache:/data:Z" \
  imgcache-worker:local
```

See [examples/compose.yaml](examples/compose.yaml) for a Compose-style deployment with a dedicated state volume.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
scripts/test_worker_container.sh                                    # podman smoke test
STRESS_DURATION_SECONDS=600 scripts/stress_worker_memory.sh         # memory stress test
```

## Notes

- Clients never write under `cache/`; workers are the only cache writers.
- TTL eviction removes old `cache/nodes/` and `cache/leaves/` files, never `cache/pinned/` or `raw/`.
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
