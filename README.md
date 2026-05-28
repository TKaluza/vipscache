# imgcache

Content-addressed derivative cache for images and PDF pages.

`imgcache` lets thin Python clients request immutable image/page derivatives while a separate worker process owns all libvips rendering and cache writes. Clients can run without `pyvips`; workers depend on system libvips through `pyvips`.

## Status

This repository is early but runnable. The current implementation includes:

- content-addressed source, node, and leaf keys
- native libvips `.v` intermediate nodes
- final `.webp`, `.png`, `.jpg`, `.avif`, `.tif`, and `.tiff` leaves
- explicit materialization policies: `never`, `force`, `pin`
- PDF page rendering through libvips
- image load/transform/encode through libvips
- ZeroMQ client/worker boundary
- Podman worker container
- TTL eviction for non-pinned cache files
- smoke and memory stress scripts

For deeper implementation notes, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Runtime Split

Install only the client dependency where you build specs and read cache hits:

```bash
pip install 'imgcache[client]'
```

Install the worker extra where rendering happens:

```bash
pip install 'imgcache[worker]'
```

The worker also needs system libvips installed. The included worker container does this for you.

## Development

This project uses `uv` and Python 3.11+.

```bash
uv sync --all-extras --dev
uv run pytest
```

## Core Concepts

Original files are identified by:

```text
file_id = xxh3-64(file bytes)
```

Every transformation node is keyed from its parent key, operation, canonical params, and engine version. Leaf outputs are keyed from their parent key, output format, encode params, and engine version.

Materialization is explicit:

```text
never  # do not write this node as .v
force  # write this node as native libvips .v
pin    # write this node under pinned/ and keep it out of normal TTL eviction
```

There is no automatic materialization policy.

## Minimal Client Example

```python
from pathlib import Path

from imgcache import ImgCacheClient

client = ImgCacheClient.zmq(
    root="/shared",
    endpoint="tcp://127.0.0.1:5555",
)

preview = (
    client.open(Path("example.jpg"), mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

path = preview.path()
print(path)
```

The client computes the expected cache path locally. On a miss, it asks the worker to materialize the derivative.

## Worker

Run a worker directly:

```bash
IMGCACHE_ROOT=/shared \
IMGCACHE_ENDPOINT='tcp://*:5555' \
uv run imgcache-zmq-worker
```

Useful defaults:

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

`IMGCACHE_LIBVIPS_CACHE_MAX_OPS=0` disables libvips' operation cache by default. `imgcache` has its own file cache, and image workers usually process many different derivatives.

## Container

Build the worker image:

```bash
podman build --format docker -f Containerfile.worker -t imgcache-worker:local .
```

Run it with a mounted shared root and a hard memory limit:

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

## Tests

Run the Python test suite:

```bash
uv run pytest
```

Run the container smoke test:

```bash
scripts/test_worker_container.sh
```

Run a longer memory stress test:

```bash
STRESS_DURATION_SECONDS=600 MEMORY_LIMIT=512m scripts/stress_worker_memory.sh
```

The stress test supports two modes:

```bash
STRESS_MODE=warm-cache scripts/stress_worker_memory.sh
STRESS_MODE=cold-derivatives scripts/stress_worker_memory.sh
```

`warm-cache` is the realistic default: derivatives are repeatedly requested and the cache warms up. `cold-derivatives` deletes the requested leaf and materialized `.v` nodes before each request, so the worker keeps doing real render/encode work.

Stress logs are written under `.stress-runs/`.

## Eviction

TTL eviction removes old files under `cache/nodes/` and `cache/leaves/` by file `mtime`. It does not delete `cache/pinned/`.

The worker exposes an internal ZMQ `evict_ttl` request, and the underlying function is:

```python
from imgcache.eviction import evict_ttl
```

## Design Notes

- ZMQ is an internal boundary, not a public REST API.
- Image bytes do not cross ZMQ; clients and workers share an imgcache root mount.
- Clients should not write cache leaves or `.v` nodes.
- Workers are the only cache writers.
- Public PDF page numbers are one-based.
- Hard memory guarantees come from container/process limits, not Python-level settings.
