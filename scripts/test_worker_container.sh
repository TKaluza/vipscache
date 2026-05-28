#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-imgcache-worker:local}"
CONTAINER_NAME="${CONTAINER_NAME:-imgcache-worker-smoke-$$}"
HOST_PORT="${HOST_PORT:-15555}"
MEMORY_LIMIT="${MEMORY_LIMIT:-512m}"
PDF_URL="${PDF_URL:-https://ontheline.trincoll.edu/images/bookdown/sample-local-pdf.pdf}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TMP_DIR/cache" "$TMP_DIR/originals"

uv run python - "$TMP_DIR/originals/source.ppm" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
width = 64
height = 48
pixels = bytearray()
for y in range(height):
    for x in range(width):
        pixels.extend((255, 0, 0) if 16 <= x < 48 and 12 <= y < 36 else (255, 255, 255))
path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)
PY

uv run python - "$PDF_URL" "$TMP_DIR/originals/sample-local-pdf.pdf" <<'PY'
from pathlib import Path
from urllib.request import Request, urlopen
import sys

request = Request(sys.argv[1], headers={"User-Agent": "imgcache-container-smoke/0.1"})
with urlopen(request, timeout=30) as response:
    Path(sys.argv[2]).write_bytes(response.read())
PY

podman build --format docker -f "$ROOT_DIR/Containerfile.worker" -t "$IMAGE_NAME" "$ROOT_DIR"

podman run \
  --detach \
  --name "$CONTAINER_NAME" \
  --memory "$MEMORY_LIMIT" \
  --publish "127.0.0.1:${HOST_PORT}:5555" \
  --env IMGCACHE_CACHE_ROOT=/cache \
  --env IMGCACHE_ENDPOINT=tcp://*:5555 \
  --env IMGCACHE_MAX_WORKERS=4 \
  --env IMGCACHE_ORIGINALS_ROOT=/originals \
  --volume "$TMP_DIR/cache:/cache:Z" \
  --volume "$TMP_DIR/originals:/originals:Z" \
  "$IMAGE_NAME" >/dev/null

uv run python - "$HOST_PORT" "$TMP_DIR/cache" "$TMP_DIR/originals/source.ppm" "$TMP_DIR/originals/sample-local-pdf.pdf" <<'PY'
from pathlib import Path
import shutil
import sys
import time

from imgcache import (
    CacheLayout,
    DerivativeSpec,
    MaterializePolicy,
    Operation,
    SourceSpec,
    ThinClient,
)
from imgcache.hash import file_id
from imgcache.zmq_client import ZmqWorkerClient

port = sys.argv[1]
host_cache = Path(sys.argv[2])
host_source = Path(sys.argv[3])
host_pdf = Path(sys.argv[4])
layout = CacheLayout(host_cache)

image_file_id = file_id(host_source)
image_store_path = host_source.parent / image_file_id
if not image_store_path.exists():
    shutil.copyfile(host_source, image_store_path)

pdf_file_id = file_id(host_pdf)
pdf_store_path = host_pdf.parent / pdf_file_id
if not pdf_store_path.exists():
    shutil.copyfile(host_pdf, pdf_store_path)

image_source = SourceSpec(
    file_id=image_file_id,
    original_path=f"/originals/{image_file_id}",
    mime="image/x-portable-pixmap",
)
image_spec = DerivativeSpec.build(
    image_source,
    [Operation("render", {"width": 32}, MaterializePolicy.FORCE)],
    Operation("encode", {"format": "png"}),
)

pdf_source = SourceSpec(
    file_id=pdf_file_id,
    original_path=f"/originals/{pdf_file_id}",
    mime="application/pdf",
)
pdf_spec = DerivativeSpec.canonical(
    pdf_source,
    [Operation("render", {"page": 1, "dpi": 75, "colorspace": "srgb"}, MaterializePolicy.FORCE)],
    Operation("encode", {"format": "png"}),
)

deadline = time.time() + 30
last_error = None
while time.time() < deadline:
    try:
        with ZmqWorkerClient(f"tcp://127.0.0.1:{port}", timeout_ms=1_000, request_retries=0) as worker:
            if not worker.healthcheck():
                raise RuntimeError("worker healthcheck failed")
            client = ThinClient(layout, worker)
            image_path = client.get(image_spec)
            pdf_path = client.get(pdf_spec)
        break
    except Exception as error:
        last_error = error
        time.sleep(0.5)
else:
    raise SystemExit(f"worker did not become ready: {last_error}")

for path in (image_path, pdf_path):
    if not path.exists():
        raise SystemExit(f"expected output path does not exist: {path}")

for spec in (image_spec, pdf_spec):
    node_path = layout.node_path(spec.nodes[0].key)
    if not node_path.exists():
        raise SystemExit(f"expected forced node does not exist: {node_path}")

print(image_path)
print(pdf_path)
PY

podman stats --no-stream --format 'worker_mem={{.MemUsage}}' "$CONTAINER_NAME"
