"""Optional cross-language test; npm ci && npm run build in clients/node first."""

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from vipscache.zmq_client import ZmqWorkerClient
from vipscache.zmq_worker import ZmqWorkerServer


def test_node_client_against_python_worker(tmp_path):
    node = Path(__file__).parents[1] / "clients/node"
    if not shutil.which("node") or not (node / "dist/index.js").exists():
        pytest.skip("build clients/node before the cross-language integration test")
    (tmp_path / "shared").mkdir()
    endpoint = f"ipc://{tmp_path}/worker.sock"
    server = ZmqWorkerServer(
        endpoint, root=tmp_path / "shared", management_dir=tmp_path / "retention"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        subprocess.run(
            ["node", "test/integration.mjs"],
            cwd=node,
            check=True,
            timeout=30,
            env={
                **os.environ,
                "VIPSCACHE_ROOT": str(tmp_path / "shared"),
                "VIPSCACHE_ENDPOINT": endpoint,
            },
        )
    finally:
        with ZmqWorkerClient(endpoint, timeout_ms=1000, request_retries=0) as client:
            client.shutdown_worker()
        thread.join(timeout=3)
        server.close()
