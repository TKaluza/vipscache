#!/usr/bin/env bash
set -euo pipefail
# Override CONTAINER_ENGINE=podman where appropriate. Both containers use an
# isolated network and a disposable shared volume; no host port is published.
engine="${CONTAINER_ENGINE:-docker}"
build_options=()
if [[ "$engine" == "podman" ]]; then build_options+=(--format docker); fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="vipscache-node-test-$$"
cleanup() {
  "$engine" rm -f "$name-worker" >/dev/null 2>&1 || true
  "$engine" volume rm "$name-data" >/dev/null 2>&1 || true
  "$engine" volume rm "$name-state" >/dev/null 2>&1 || true
  "$engine" network rm "$name-net" >/dev/null 2>&1 || true
}
trap cleanup EXIT
"$engine" build "${build_options[@]}" -f "$repo/Containerfile.worker" -t vipscache-worker:node-test "$repo"
"$engine" build "${build_options[@]}" -f "$repo/clients/node/Containerfile.test" -t vipscache-node:test "$repo"
"$engine" network create "$name-net" >/dev/null
"$engine" volume create "$name-data" >/dev/null
"$engine" volume create "$name-state" >/dev/null
"$engine" run -d --name "$name-worker" --network "$name-net" --network-alias worker \
  -e VIPSCACHE_ROOT=/data -e 'VIPSCACHE_ENDPOINT=tcp://*:5555' -e VIPSCACHE_MAX_WORKERS=2 \
  -e VIPSCACHE_MANAGEMENT_DIR=/state/retention \
  -v "$name-data:/data" -v "$name-state:/state" vipscache-worker:node-test >/dev/null
for attempt in {1..30}; do
  if "$engine" exec "$name-worker" vipscache-zmq-healthcheck --timeout-ms 1000; then break; fi
  if [[ "$attempt" == 30 ]]; then "$engine" logs "$name-worker"; exit 1; fi
  sleep 1
done
"$engine" run --rm --network "$name-net" -v "$name-data:/shared" \
  -e VIPSCACHE_ROOT=/shared -e VIPSCACHE_ENDPOINT=tcp://worker:5555 vipscache-node:test
