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
    client.open("example.jpg", mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

path = preview.path()     # materializes on miss, returns a local Path
blob = preview.bytes()
```

Every transformation returns a new immutable `CachedImage`; nothing renders until a path or bytes are requested. Operations: `page`, `normalize`, `scale`, `resize`, `crop`, `crop_fraction`, `rotate`, `fast_rotate`, `flip`, `flop`. Encodes: `webp`, `png`, `jpg`, `avif`, `tif`. A transformed image without an encode raises when materialized; an untransformed image resolves to the original under `raw/`.

PDFs stay PDFs until a page is selected:

```python
page = client.open("doc.pdf", mime="application/pdf").page(1, dpi=144).png()
```

Async uses the same object model:

```python
image = await client.aopen("example.jpg", mime="image/jpeg")
preview = image.scale(longest_edge=1024).webp()
path = await preview            # same as await preview.apath()
data = await preview.abytes()
```

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
 
 ## Serving to Browsers
 
 imgcache derivatives can be served to browsers through a two-plane web
 delivery tier: a ZMQ control plane (spec → relpath) and a Caddy data plane
 (read-only `sendfile` with signed URLs). See the
 [Web delivery plan](web_delivery_plan_v1.md) for the full design and the
 [Web delivery tier](ARCHITECTURE.md#web-delivery-tier) section in
 ARCHITECTURE.md for invariants.
 
 ### Quick start with Docker Compose
 
 The example stack in `examples/compose.yaml` runs three services:
 
 - **worker** — imgcache ZMQ render worker (only cache writer)
 - **sveltekit** — SvelteKit app with the verifier route and `@imgcache/client`
 - **caddy** — reverse proxy serving `cache/leaves/` (signed) and
   `cache/pinned/` (public) over HTTP/2 and HTTP/3
 
 ```bash
 # Set a real signing secret (32+ random bytes)
 export IMGCACHE_URL_SIGNING_SECRET="$(openssl rand -hex 32)"
 
 # Build and start the stack
 docker compose -f examples/compose.yaml up --build
 ```
 
 Caddy listens on ports 80 and 443 (TCP + UDP for HTTP/3). The SvelteKit app
 is internal only (port 3000, not published). The worker is internal only
 (port 5555, not published).
 
 ### Environment variables
 
 | Variable | Service | Purpose |
 |---|---|---|
 | `IMGCACHE_URL_SIGNING_SECRET` | sveltekit | HMAC secret for signing leaf URLs (32+ bytes) |
 | `IMGCACHE_URL_SIGNING_SECRET_PREVIOUS` | sveltekit | Previous secret, accepted during rotation |
 | `IMGCACHE_ZMQ_ENDPOINT` | sveltekit | Worker ZMQ address (default: `tcp://worker:5555`) |
 | `IMGCACHE_PUBLIC_BASE_URL` | sveltekit | Public origin for image URLs (e.g. `https://images.example.com`) |
 | `IMGCACHE_ENDPOINT` | worker | ZMQ bind address (default: `tcp://*:5555`) |
 | `IMGCACHE_ROOT` | worker | Shared cache root (default: `/data`) |
 
 ### Using `@imgcache/client`
 
 The TypeScript client (`clients/typescript/`) is a **server-side-only**
 package — it uses native ZMQ sockets and must never be bundled for the
 browser.
 
 ```bash
 cd clients/typescript && npm install && npm run build
 # In your SvelteKit app:
 npm install /path/to/imgcache/clients/typescript
 ```
 
 ```ts
 import { ImgCache, signUrl, publishUrl } from "@imgcache/client";
 
 // Server-side singleton (e.g. in $lib/server/imgcache.ts)
 const cache = new ImgCache({
   endpoint: "tcp://worker:5555",
   timeoutMs: 300_000,
   retries: 2,
 });
 
 // Resolve a spec → get relpath → sign a URL for the browser
 const spec = cache.open(fileId, "application/pdf")
   .page(1, { dpi: 144 })
   .scale({ longestEdge: 1024 })
   .webp({ quality: 82 });
 const { relpath } = await spec.resolve();
 const url = signUrl(relpath, { secret: process.env.IMGCACHE_URL_SIGNING_SECRET! });
 
 // Public (pinned) tier — unsigned URL, excluded from eviction
 const pubUrl = await publishUrl(cache, spec);
 ```
 
 See `examples/sveltekit/` for a complete reference integration including
 `srcset` patterns, the verifier route, and deployment notes.
 
 ### Load testing
 
 ```bash
 # Dry-run (validates URL signing locally, no live endpoint needed)
 python scripts/load_test.py
 
 # Live (point at a running stack)
 IMGCACHE_LOAD_TEST_ENDPOINT=https://images.example.com \
 IMGCACHE_LOAD_TEST_SECRET="$IMGCACHE_URL_SIGNING_SECRET" \
 IMGCACHE_LOAD_TEST_LEAF_PATH=/cache/leaves/e7/abc...webp \
 IMGCACHE_LOAD_TEST_PINNED_PATH=/cache/pinned/cd/def...webp \
 IMGCACHE_LOAD_TEST_DRY_RUN=0 \
 python scripts/load_test.py
 ```
