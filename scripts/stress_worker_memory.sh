#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-imgcache-worker:local}"
CONTAINER_NAME="${CONTAINER_NAME:-imgcache-worker-stress-$$}"
HOST_PORT="${HOST_PORT:-15556}"
MEMORY_LIMIT="${MEMORY_LIMIT:-512m}"
MEMORY_SWAP="${MEMORY_SWAP:-$MEMORY_LIMIT}"
MAX_WORKERS="${MAX_WORKERS:-4}"
STRESS_CONCURRENCY="${STRESS_CONCURRENCY:-4}"
STRESS_DURATION_SECONDS="${STRESS_DURATION_SECONDS:-600}"
STRESS_MODE="${STRESS_MODE:-warm-cache}"
STATS_INTERVAL_SECONDS="${STATS_INTERVAL_SECONDS:-2}"
PDF_URL="${PDF_URL:-https://ontheline.trincoll.edu/images/bookdown/sample-local-pdf.pdf}"
IMAGE_URL="${IMAGE_URL:-https://commons.wikimedia.org/wiki/Special:Redirect/file/Example_image_not_to_be_used_in_article_namespace.jpg}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/.stress-runs/$(date +%Y%m%d-%H%M%S)}"
STATS_LOG="$RUN_DIR/stats.csv"
WORKLOAD_LOG="$RUN_DIR/workload.json"

cleanup() {
  if [[ -n "${STATS_PID:-}" ]]; then
    kill "$STATS_PID" >/dev/null 2>&1 || true
    wait "$STATS_PID" >/dev/null 2>&1 || true
  fi
  podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$RUN_DIR/shared" "$RUN_DIR/input"

if [[ "$STRESS_MODE" != "warm-cache" && "$STRESS_MODE" != "cold-derivatives" ]]; then
  echo "STRESS_MODE must be warm-cache or cold-derivatives, got: $STRESS_MODE" >&2
  exit 2
fi

uv run python - "$PDF_URL" "$RUN_DIR/input/sample-local-pdf.pdf" "$IMAGE_URL" "$RUN_DIR/input/example.jpg" <<'PY'
from pathlib import Path
from urllib.request import Request, urlopen
import sys


def download(url: str, path: Path) -> None:
    request = Request(url, headers={"User-Agent": "imgcache-memory-stress/0.1"})
    with urlopen(request, timeout=60) as response:
        path.write_bytes(response.read())


download(sys.argv[1], Path(sys.argv[2]))
try:
    download(sys.argv[3], Path(sys.argv[4]))
except Exception:
    width = 2048
    height = 1536
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend(((x * 255) // width, (y * 255) // height, 180))
    Path(sys.argv[4]).write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)
PY

podman build --format docker -f "$ROOT_DIR/Containerfile.worker" -t "$IMAGE_NAME" "$ROOT_DIR"

podman run \
  --detach \
  --name "$CONTAINER_NAME" \
  --memory "$MEMORY_LIMIT" \
  --memory-swap "$MEMORY_SWAP" \
  --publish "127.0.0.1:${HOST_PORT}:5555" \
  --env IMGCACHE_ENDPOINT=tcp://*:5555 \
  --env IMGCACHE_MAX_WORKERS="$MAX_WORKERS" \
  --env IMGCACHE_ROOT=/data \
  --volume "$RUN_DIR/shared:/data:Z" \
  "$IMAGE_NAME" >/dev/null

printf 'timestamp,mem_usage\n' >"$STATS_LOG"
(
  while podman container exists "$CONTAINER_NAME" >/dev/null 2>&1; do
    printf '%s,%s\n' "$(date -Is)" "$(podman stats --no-stream --format '{{.MemUsage}}' "$CONTAINER_NAME" 2>/dev/null || true)" >>"$STATS_LOG"
    sleep "$STATS_INTERVAL_SECONDS"
  done
) &
STATS_PID="$!"

uv run python - \
  "$HOST_PORT" \
  "$RUN_DIR/shared" \
  "$RUN_DIR/input/example.jpg" \
  "$RUN_DIR/input/sample-local-pdf.pdf" \
  "$STRESS_DURATION_SECONDS" \
  "$STRESS_CONCURRENCY" \
  "$STRESS_MODE" \
  "$WORKLOAD_LOG" <<'PY'
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import sys
import threading
import time

from imgcache import CacheLayout, ImageSpec, ImgCacheClient, MaterializePolicy, Operation
from imgcache.zmq_client import ZmqWorkerClient


port = sys.argv[1]
root = Path(sys.argv[2])
image_path = Path(sys.argv[3])
pdf_path = Path(sys.argv[4])
duration_seconds = int(sys.argv[5])
concurrency = int(sys.argv[6])
stress_mode = sys.argv[7]
workload_log = Path(sys.argv[8])
endpoint = f"tcp://127.0.0.1:{port}"
layout = CacheLayout(root / "cache")


ingest_client = ImgCacheClient(root)
image_source = ingest_client.open(image_path, mime="image/jpeg").source
pdf_source = ingest_client.open(pdf_path, mime="application/pdf").source


deadline = time.monotonic() + 60
last_error = None
while time.monotonic() < deadline:
    try:
        with ZmqWorkerClient(endpoint, timeout_ms=1_000, request_retries=0) as client:
            if client.healthcheck():
                break
    except Exception as error:
        last_error = error
    time.sleep(0.5)
else:
    raise SystemExit(f"worker did not become ready: {last_error}")


def image_spec(index: int) -> ImageSpec:
    longest = 320 + (index % 21) * 24
    quality = 68 + (index % 20)
    rotate = (index % 4) * 90
    operations = [
        Operation("normalize", {"colorspace": "srgb"}),
        Operation("fast_rotate", {"degrees": rotate}),
        Operation("scale", {"longest_edge": longest}, MaterializePolicy.FORCE if index % 11 == 0 else MaterializePolicy.NEVER),
    ]
    return ImageSpec.canonical(
        image_source,
        operations,
        Operation("encode", {"format": "webp", "quality": quality}),
    )


def pdf_spec(index: int) -> ImageSpec:
    dpi = [72, 90, 110, 130][index % 4]
    longest = 420 + (index % 19) * 30
    quality = 70 + (index % 18)
    operations = [
        Operation("render", {"page": 1, "dpi": dpi, "colorspace": "srgb"}, MaterializePolicy.FORCE if index % 9 == 0 else MaterializePolicy.NEVER),
        Operation("scale", {"longest_edge": longest}),
    ]
    return ImageSpec.canonical(
        pdf_source,
        operations,
        Operation("encode", {"format": "webp", "quality": quality}),
    )


counter_lock = threading.Lock()
counter = 0
errors: list[str] = []


def next_index() -> int:
    global counter
    with counter_lock:
        value = counter
        counter += 1
        return value


def remove_cached_outputs(spec: ImageSpec) -> None:
    layout.leaf_path(spec.leaf.key, spec.leaf.extension).unlink(missing_ok=True)
    for node in spec.nodes:
        if node.materialize in {MaterializePolicy.FORCE, MaterializePolicy.PIN}:
            layout.node_path(node.key, pinned=node.materialize == MaterializePolicy.PIN).unlink(missing_ok=True)


def worker_loop() -> int:
    completed = 0
    with ZmqWorkerClient(endpoint, timeout_ms=120_000, request_retries=1) as worker_client:
        client = ImgCacheClient(root, worker_client)
        while time.monotonic() < stop_at:
            index = next_index()
            spec = pdf_spec(index) if index % 2 == 0 else image_spec(index)
            try:
                if stress_mode == "cold-derivatives":
                    remove_cached_outputs(spec)
                path = client.get(spec)
                if not path.exists():
                    raise RuntimeError(f"missing output: {path}")
                completed += 1
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
    return completed


stop_at = time.monotonic() + duration_seconds
started = time.time()
completed = 0
with ThreadPoolExecutor(max_workers=concurrency) as executor:
    futures = [executor.submit(worker_loop) for _ in range(concurrency)]
    for future in as_completed(futures):
        completed += future.result()

summary = {
    "cache_bytes": sum(path.stat().st_size for path in layout.root.rglob("*") if path.is_file()),
    "completed": completed,
    "duration_seconds": round(time.time() - started, 3),
    "errors": errors[:20],
    "error_count": len(errors),
    "mode": stress_mode,
}
workload_log.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps(summary, indent=2, sort_keys=True))
PY

kill "$STATS_PID" >/dev/null 2>&1 || true
wait "$STATS_PID" >/dev/null 2>&1 || true
unset STATS_PID

uv run python - "$STATS_LOG" "$WORKLOAD_LOG" "$RUN_DIR/shared/cache" <<'PY'
from __future__ import annotations

from pathlib import Path
import json
import re
import sys


stats_log = Path(sys.argv[1])
workload_log = Path(sys.argv[2])
cache_root = Path(sys.argv[3])


def to_mib(value: str) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMG]i?B|[KMG]B)", value)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit in {"kb", "kib"}:
        return amount / 1024
    if unit in {"gb", "gib"}:
        return amount * 1024
    return amount


samples: list[float] = []
for line in stats_log.read_text(encoding="utf-8").splitlines()[1:]:
    parts = line.split(",", 1)
    if len(parts) != 2:
        continue
    mib = to_mib(parts[1])
    if mib is not None:
        samples.append(mib)

workload = json.loads(workload_log.read_text(encoding="utf-8"))
print()
print("memory stress summary")
print(f"  mode: {workload['mode']}")
print(f"  completed: {workload['completed']}")
print(f"  errors: {workload['error_count']}")
print(f"  cache_bytes: {workload['cache_bytes']}")
print(f"  cache_files: {sum(1 for path in cache_root.rglob('*') if path.is_file())}")
if samples:
    print(f"  mem_samples: {len(samples)}")
    print(f"  mem_peak_mib: {max(samples):.2f}")
    print(f"  mem_last_mib: {samples[-1]:.2f}")
    print(f"  mem_first_mib: {samples[0]:.2f}")
print(f"  stats_log: {stats_log}")
print(f"  workload_log: {workload_log}")
PY
