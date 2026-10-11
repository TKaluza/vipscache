import asyncio
import time
from threading import Thread

import pytest
from test_zmq import make_image

from vipscache import (
    ImageSpec,
    Operation,
    SourceSpec,
    VipsCacheClient,
    VipsCacheError,
    WorkerLimits,
)
from vipscache.zmq_client import ZmqWorkerClient
from vipscache.zmq_worker import ZmqWorkerServer


def test_typed_errors_over_zmq_and_client_reuse(tmp_path):
    root = tmp_path / "shared"
    endpoint = f"ipc://{tmp_path}/worker.sock"
    server = ZmqWorkerServer(endpoint, root=root, limits=WorkerLimits(max_input_pixels=100))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = ZmqWorkerClient(endpoint, timeout_ms=1000, request_retries=0)
    client = VipsCacheClient(root, worker)
    bad = tmp_path / "invalid"
    bad.write_bytes(b"not an image")
    good = tmp_path / "source.ppm"
    make_image(good)
    cases = [
        (
            ImageSpec.build(SourceSpec("a" * 32), [], Operation("encode", {"format": "png"})),
            "OriginalMissing",
        ),
        (client.register(bad).png().spec, "UnreadableFormat"),
        (client.register(good).thumbnail(longest_edge=8).png().spec, "LimitExceeded"),
        (
            ImageSpec.build(SourceSpec("a" * 32), [], Operation("encode", {"format": "invalid"})),
            "InvalidSpec",
        ),
    ]
    try:
        for spec, category in cases:
            with pytest.raises(VipsCacheError) as caught:
                worker.materialize(spec)
            assert caught.value.type == category
            assert worker.healthcheck()
        with ZmqWorkerClient(
            f"ipc://{tmp_path}/absent.sock", timeout_ms=20, request_retries=0
        ) as absent:
            with pytest.raises(VipsCacheError) as caught:
                absent.healthcheck()
            assert caught.value.type == "Timeout"
            assert isinstance(caught.value, TimeoutError)
    finally:
        worker.shutdown_worker()
        thread.join(5)
        worker.close()
        server.close()


def test_cancelled_async_request_does_not_poison_client(tmp_path, monkeypatch):
    endpoint = f"ipc://{tmp_path}/worker.sock"
    server = ZmqWorkerServer(endpoint, root=tmp_path / "shared")
    original_dispatch = server._dispatch

    def delayed(request, **kwargs):
        if request.get("method") == "identify":
            time.sleep(0.15)
        return original_dispatch(request, **kwargs)

    monkeypatch.setattr(server, "_dispatch", delayed)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ZmqWorkerClient(endpoint, timeout_ms=1000, request_retries=0)

    async def run():
        task = asyncio.create_task(client.aidentify(ImageSpec(SourceSpec("a" * 32))))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        response = await client._arequest({"method": "health"})
        assert response["ok"]

    try:
        asyncio.run(run())
    finally:
        client.shutdown_worker()
        thread.join(5)
        client.close()
        server.close()


def test_ensure_does_not_silently_succeed_on_old_worker(tmp_path, monkeypatch):
    endpoint = f"ipc://{tmp_path}/worker.sock"
    server = ZmqWorkerServer(endpoint, root=tmp_path / "shared")
    original_dispatch = server._dispatch

    def old_dispatch(request, **kwargs):
        if request.get("method") == "ensure":
            return {
                "ok": False,
                "error": {"type": "ValueError", "message": "unsupported method: ensure"},
            }
        return original_dispatch(request, **kwargs)

    monkeypatch.setattr(server, "_dispatch", old_dispatch)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = ZmqWorkerClient(endpoint)
    client = VipsCacheClient(tmp_path / "shared", worker)
    file = tmp_path / "source.ppm"
    make_image(file)
    source = client.register(file)
    existing = source.png().path()
    try:
        with pytest.raises(VipsCacheError, match="unsupported method"):
            source.png().ensure(retain="pin")
        assert existing.exists()
    finally:
        worker.shutdown_worker()
        thread.join(5)
        worker.close()
        server.close()


def test_async_ensure_unpin_delete_over_zmq(tmp_path):
    endpoint = f"ipc://{tmp_path}/async-management.sock"
    root = tmp_path / "shared"
    server = ZmqWorkerServer(endpoint, root=root, management_dir=tmp_path / "retention")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = ZmqWorkerClient(endpoint, timeout_ms=1000, request_retries=0)
    client = VipsCacheClient(root, worker)
    source = tmp_path / "source.ppm"
    make_image(source)

    async def run():
        image = (await client.aregister(source)).scale(width=32).materialize("force").png()
        path = await image.aensure(retain="pin")
        data = await image.aread_bytes()
        assert data.startswith(b"\x89PNG")
        await image.aunpin()
        await client.adelete([image])
        assert not path.exists()
        await image.adelete()
        assert await image.aensure() == path
        assert await image.aread_bytes() == data

    try:
        asyncio.run(run())
    finally:
        worker.shutdown_worker()
        thread.join(5)
        worker.close()
        server.close()
