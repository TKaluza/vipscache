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

mkdir -p "$TMP_DIR/shared" "$TMP_DIR/input"

uv run python - "$TMP_DIR/input/source.ppm" <<'PY'
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

uv run python - "$PDF_URL" "$TMP_DIR/input/sample-local-pdf.pdf" <<'PY'
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
  --env IMGCACHE_ENDPOINT=tcp://*:5555 \
  --env IMGCACHE_MAX_WORKERS=4 \
  --env IMGCACHE_ROOT=/data \
  --volume "$TMP_DIR/shared:/data:Z" \
  "$IMAGE_NAME" >/dev/null

uv run python - "$HOST_PORT" "$TMP_DIR/shared" "$TMP_DIR/input/source.ppm" "$TMP_DIR/input/sample-local-pdf.pdf" <<'PY'
from pathlib import Path
import sys
import time

from imgcache import ImgCacheClient
from imgcache.zmq_client import ZmqWorkerClient

port = sys.argv[1]
root = Path(sys.argv[2])
host_source = Path(sys.argv[3])
host_pdf = Path(sys.argv[4])

deadline = time.time() + 30
last_error = None
while time.time() < deadline:
    try:
        with ZmqWorkerClient(f"tcp://127.0.0.1:{port}", timeout_ms=1_000, request_retries=0) as worker:
            if not worker.healthcheck():
                raise RuntimeError("worker healthcheck failed")
            client = ImgCacheClient(root, worker)
            image_path = client.open(host_source, mime="image/x-portable-pixmap").scale(width=32).png().path()
            pdf_path = client.open(host_pdf, mime="application/pdf").page(1, dpi=75).png().path()
        break
    except Exception as error:
        last_error = error
        time.sleep(0.5)
else:
    raise SystemExit(f"worker did not become ready: {last_error}")

for path in (image_path, pdf_path):
    if not path.exists():
        raise SystemExit(f"expected output path does not exist: {path}")

print(image_path)
print(pdf_path)
PY

podman stats --no-stream --format 'worker_mem={{.MemUsage}}' "$CONTAINER_NAME"
