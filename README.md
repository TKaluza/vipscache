# imgcache

Content-addressed image and PDF-page derivatives with a lazy, image-like Python API.

`imgcache` keeps clients thin: client code ingests originals, builds immutable specs, checks cache paths, and asks a worker to render cache misses. Rendering and libvips stay on the worker side.

## Shape

Every cache root has one layout:

```text
<root>/
  raw/
    <file_id>
  cache/
    nodes/
    pinned/
    leaves/
```

Originals are copied to `raw/<file_id>`, where `file_id` is `xxh3-64(file bytes)`. Derived files are keyed from the source, ordered operations, encode parameters, and engine version.

## Install

Client-only environment:

```bash
pip install 'imgcache[client]'
```

Worker environment:

```bash
pip install 'imgcache[worker]'
```

Workers also need system libvips. The included container image installs it.

## Client API

```python
from PIL import Image

from imgcache import ImgCacheClient

client = ImgCacheClient.zmq(
    root="/shared/imgcache",
    endpoint="tcp://127.0.0.1:5555",
)

preview = (
    client.open("example.jpg", mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

path = preview.path()
blob = preview.bytes()
pil = Image.open(preview)
```

An unchanged `CachedImage` resolves to the original under `raw/`:

```python
original = client.open("example.jpg", mime="image/jpeg")
print(original.path())
```

PDFs stay PDFs until a page is selected:

```python
page = (
    client.open("invoice.pdf", mime="application/pdf")
    .page(1, dpi=144)
    .scale(longest_edge=1024)
    .png()
)
```

Async uses the same object model:

```python
image = await client.aopen("example.jpg", mime="image/jpeg")
preview = image.scale(longest_edge=1024).webp(quality=82)

path = await preview
blob = await preview.abytes()
```

Transformations must choose an output format before materialization:

```python
client.open("example.jpg", mime="image/jpeg").scale(longest_edge=1024).path()
# ValueError: transformed CachedImage must choose an output format...
```

## Async API

Async keeps the same lazy pipeline. Building the pipeline is still synchronous and cheap; only ingest, worker materialization, and byte reads are awaited.

```python
client = ImgCacheClient.zmq(
    root="/shared/imgcache",
    endpoint="tcp://127.0.0.1:5555",
)

original = await client.aopen("example.jpg", mime="image/jpeg")

preview = (
    original
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

path = await preview
same_path = await preview.apath()
data = await preview.abytes()
```

Available async entry points:

| Method | Description |
| --- | --- |
| `await client.aopen(path, mime=None, metadata=None)` | Ingests an original without blocking the event loop; hashing/copying runs in a thread. |
| `await client.aget(spec)` | Async materialization for an `ImageSpec`. |
| `await image.apath()` | Returns the materialized `Path`. |
| `await image.abytes()` | Materializes, then reads bytes in a thread. |
| `await image` | Shortcut for `await image.apath()`. |

When the client is created with `ImgCacheClient.zmq(...)`, async materialization uses `zmq.asyncio`. If the local file is already cached, `apath()` returns the path without a worker request.

## Available Methods

`ImgCacheClient`:

| Method | Description |
| --- | --- |
| `ImgCacheClient.zmq(root, endpoint, request_retries=2, timeout_ms=300_000)` | Creates a client that uses a ZMQ render worker. |
| `client.open(path, mime=None, metadata=None)` | Copies a local original into `<root>/raw/<file_id>` and returns a `CachedImage`. |
| `await client.aopen(path, mime=None, metadata=None)` | Async wrapper for ingesting an original. |
| `client.get(spec)` | Returns the local path for an `ImageSpec`, materializing cache misses through the worker. |
| `await client.aget(spec)` | Async materialization path for an `ImageSpec`. |

`CachedImage` transformations are immutable: every call returns a new `CachedImage`.

| Method | Description |
| --- | --- |
| `.page(page=1, dpi=None, **params)` | Selects/renders a PDF page. Page numbers are one-based. Optional params include `n`, `colorspace`, `background`, and one size strategy such as `dpi`, `width`, `height`, `longest_edge`, or `scale_factor`. |
| `.normalize(colorspace="srgb")` | Converts the image to a stable color space. Supported color spaces are `srgb`/`rgb` and grayscale aliases such as `gray`, `grey`, or `b-w`. |
| `.scale(longest_edge=..., width=..., height=..., scale_factor=...)` | Resizes while preserving aspect ratio unless both `width` and `height` are given. Exactly one size strategy should be used. |
| `.resize(width=..., height=..., longest_edge=..., scale_factor=...)` | Same resize engine as `.scale(...)`; useful when the call site wants resize wording. |
| `.crop(x=..., y=..., w=..., h=...)` | Crops a rectangle from the current image. Coordinates are pixel-based and validated against the libvips size. |
| `.crop_fraction(left=0.0, top=0.0, right=1.0, bottom=1.0)` | Crops a normalized `0..1` box resolved against the libvips pixel size on the worker. Resolution-independent; the caller computes no pixels. |
| `.fast_rotate(degrees)` | Lossless-style right-angle rotation. `degrees` must be `0`, `90`, `180`, or `270`. |
| `.rotate(degrees)` | Arbitrary-angle rotation using interpolation. |
| `.flip()` | Vertical flip. |
| `.flop()` | Horizontal flip. |

Encode methods choose the output format and make the pipeline materializable:

| Method | Description |
| --- | --- |
| `.webp(quality=82, **params)` | Encodes as WebP. |
| `.png(**params)` | Encodes as PNG. |
| `.jpg(quality=85, **params)` | Encodes as JPEG. Alpha is flattened first. |
| `.avif(quality=60, **params)` | Encodes as AVIF. |
| `.tif(**params)` | Encodes as TIFF. |

Materialization and read methods:

| Method | Description |
| --- | --- |
| `.path()` | Returns a `Path`. On cache miss, asks the worker to materialize the leaf. |
| `await .apath()` | Async path/materialization. |
| `.open("rb")` | Opens the materialized file. |
| `.bytes()` | Reads the materialized file as bytes. |
| `await .abytes()` | Async bytes read. |
| `Image.open(image)` | Works because `CachedImage` implements `__fspath__`. |
| `await image` | Returns the same `Path` as `await image.apath()`. |

## Metadata

Every `CachedImage` exposes Pillow-like metadata derived from libvips. The worker
computes it lazily over the *same* render pipeline it would use to materialize, so
the reported geometry always matches the file you would get — libvips owns pixel
geometry, including PDF `/Rotate` and EXIF orientation.

```python
page = client.open("scan.pdf", mime="application/pdf").page(1, dpi=75)

page.size        # (620, 876) — rotation already applied by libvips
page.width       # 620
page.mode        # "RGB"
page.info        # {"width": 620, "height": 876, "bands": 3, "dpi": [75.0, 75.0], ...}
page.n_pages     # 1
info = await page.ainfo()
```

| Member | Description |
| --- | --- |
| `.size` / `.width` / `.height` | Pixel geometry of this exact pipeline (post-transform). |
| `.mode` | Pillow-style mode (`RGB`, `RGBA`, `L`, `LA`, `CMYK`). |
| `.info` | Full metadata dict (bands, interpretation, alpha, dpi, orientation, n_pages, format). |
| `.n_pages` | Page count; works on a PDF original before a page is selected. |
| `.identify()` / `await .ainfo()` | Force a metadata fetch and return the dict. |

Metadata is fetched once over ZMQ on first access and cached on the (immutable)
object; deriving a new `CachedImage` re-fetches for the new pipeline. Reading
`.size` or `.mode` requires a worker, and for a PDF source it requires a selected
page (`.page(...)`); `.n_pages` does not.

Because imgcache reports the true rendered size, a consumer cropping a rotated PDF
page no longer has to compute geometry itself:

```python
top_half = page.crop_fraction(bottom=0.5)   # never produces "bad extract area"
```

## Worker

Run a worker against the same shared root:

```bash
IMGCACHE_ROOT=/shared/imgcache \
IMGCACHE_ENDPOINT='tcp://*:5555' \
uv run imgcache-zmq-worker
```

Important defaults:

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

Healthcheck:

```bash
uv run imgcache-zmq-healthcheck --endpoint tcp://127.0.0.1:5555
```

## Container

Build:

```bash
podman build --format docker -f Containerfile.worker -t imgcache-worker:local .
```

Run:

```bash
podman run --rm \
  --memory 512m \
  --memory-swap 512m \
  -p 127.0.0.1:5555:5555 \
  -e IMGCACHE_ROOT=/data \
  -e IMGCACHE_ENDPOINT='tcp://*:5555' \
  -v "$PWD/.local/imgcache:/data:Z" \
  imgcache-worker:local
```

For Compose-style deployment, see [examples/compose.yaml](examples/compose.yaml).

## Development

```bash
uv sync --all-extras --dev
uv run pytest
```

Container smoke test:

```bash
scripts/test_worker_container.sh
```

Memory stress test:

```bash
STRESS_DURATION_SECONDS=600 MEMORY_LIMIT=512m scripts/stress_worker_memory.sh
```

## Notes

- Clients never write `cache/`; workers are the only cache writers.
- Image bytes do not cross ZMQ; specs and relative paths do.
- Public PDF page numbers are one-based.
- TTL eviction removes old files under `cache/nodes/` and `cache/leaves/`, but not `cache/pinned/` or `raw/`.
- Details live in [ARCHITECTURE.md](ARCHITECTURE.md).
