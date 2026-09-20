#!/usr/bin/env bash
set -euo pipefail
# Override CONTAINER_ENGINE=podman where appropriate. Both containers use an
# isolated network and a disposable shared volume; no host port is published.
engine="${CONTAINER_ENGINE:-docker}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="imgcache-node-test-$$"
cleanup() {
  "$engine" rm -f "$name-worker" >/dev/null 2>&1 || true
  "$engine" volume rm "$name-data" >/dev/null 2>&1 || true
  "$engine" network rm "$name-net" >/dev/null 2>&1 || true
}
trap cleanup EXIT
"$engine" build -f "$repo/Containerfile.worker" -t imgcache-worker:node-test "$repo"
"$engine" build -f "$repo/clients/node/Containerfile.test" -t imgcache-node:test "$repo"
"$engine" network create "$name-net" >/dev/null
"$engine" volume create "$name-data" >/dev/null
"$engine" run -d --name "$name-worker" --network "$name-net" --network-alias worker \
  -e IMGCACHE_ROOT=/data -e 'IMGCACHE_ENDPOINT=tcp://*:5555' -e IMGCACHE_MAX_WORKERS=2 \
  -v "$name-data:/data" imgcache-worker:node-test >/dev/null
for attempt in {1..30}; do
  if "$engine" exec "$name-worker" imgcache-zmq-healthcheck --timeout-ms 1000; then break; fi
  if [[ "$attempt" == 30 ]]; then "$engine" logs "$name-worker"; exit 1; fi
  sleep 1
done
"$engine" run --rm --network "$name-net" -v "$name-data:/shared" \
  -e IMGCACHE_ROOT=/shared -e IMGCACHE_ENDPOINT=tcp://worker:5555 imgcache-node:test
