from __future__ import annotations

import time
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest

from imgcache import CacheLayout, ImgCacheClient, MaterializePolicy, Operation, RenderWorker, SourceSpec
from imgcache.executor import VipsExecutor
from imgcache.spec import ImageSpec
from imgcache.zmq_client import ZmqWorkerClient
from imgcache.zmq_worker import ZmqWorkerServer


def make_image(path: Path) -> None:
    width = 32
    height = 24
    pixels = bytearray()
    for _ in range(width * height):
        pixels.extend((0, 128, 255))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)


def test_img_cache_client_materializes_miss_over_zmq(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    root = tmp_path / "shared"
    endpoint = f"ipc://{tmp_path / 'worker.sock'}"
    server = ZmqWorkerServer(endpoint, root=root)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with ZmqWorkerClient(endpoint) as worker_client:
        client = ImgCacheClient(root, worker_client)
        image = client.open(source_path, mime="image/x-portable-pixmap")
        preview = image.scale(width=16).png()
        path = preview.path()
        worker_client.shutdown_worker()

    thread.join(timeout=5)
    server.close()

    assert path.exists()
    assert path.is_relative_to(root / "cache" / "leaves")


def test_img_cache_client_identify_over_zmq(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    root = tmp_path / "shared"
    endpoint = f"ipc://{tmp_path / 'identify.sock'}"
    server = ZmqWorkerServer(endpoint, root=root)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with ZmqWorkerClient(endpoint) as worker_client:
        client = ImgCacheClient(root, worker_client)
        image = client.open(source_path, mime="image/x-portable-pixmap").scale(width=16)
        meta = image.identify()
        size = image.size
        worker_client.shutdown_worker()

    thread.join(timeout=5)
    server.close()

    assert meta["width"] == 16
    assert meta["mode"] == "RGB"
    assert size == (16, 12)  # 32x24 scaled to width 16


def test_zmq_worker_healthcheck(tmp_path):
    endpoint = f"ipc://{tmp_path / 'health.sock'}"
    server = ZmqWorkerServer(endpoint, root=tmp_path / "shared")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with ZmqWorkerClient(endpoint) as worker_client:
        assert worker_client.healthcheck()
        worker_client.shutdown_worker()

    thread.join(timeout=5)
    server.close()


def test_zmq_worker_stats(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    root = tmp_path / "shared"
    endpoint = f"ipc://{tmp_path / 'stats.sock'}"
    server = ZmqWorkerServer(endpoint, root=root, state_dir=tmp_path / "state")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with ZmqWorkerClient(endpoint) as worker_client:
        client = ImgCacheClient(root, worker_client)
        source = client.open(source_path, mime="image/x-portable-pixmap").source
        spec = ImageSpec.build(
            source,
            [Operation("scale", {"width": 16}, MaterializePolicy.FORCE)],
            Operation("encode", {"format": "png"}),
        )
        path = client.get(spec)
        top = worker_client.stats(top=1)
        inspected = worker_client.stats(key=source.file_id)
        worker_client.shutdown_worker()

    thread.join(timeout=5)
    server.close()

    assert path.exists()
    assert top["nodes"][0]["key"] == spec.nodes[0].key
    assert top["nodes"][0]["builds"] == 1
    assert inspected["children"][0]["key"] == spec.nodes[0].key


def test_zmq_worker_stats_disabled(tmp_path):
    endpoint = f"ipc://{tmp_path / 'stats-disabled.sock'}"
    server = ZmqWorkerServer(endpoint, root=tmp_path / "shared")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with ZmqWorkerClient(endpoint) as worker_client:
        with pytest.raises(RuntimeError, match="StateDisabled"):
            worker_client.stats(top=1)
        worker_client.shutdown_worker()

    thread.join(timeout=5)
    server.close()


class SlowLeafExecutor(VipsExecutor):
    """Delays leaf writes so a parallel duplicate request hits the busy path."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.leaf_writes = 0

    def write_leaf(self, image, spec, path):
        self.leaf_writes += 1
        time.sleep(self.delay)
        super().write_leaf(image, spec, path)


def test_zmq_busy_duplicate_requests_resolve_transparently(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    root = tmp_path / "shared"
    endpoint = f"ipc://{tmp_path / 'busy.sock'}"

    source = ImgCacheClient(root).open(source_path, mime="image/x-portable-pixmap").source
    spec = ImageSpec.build(
        source,
        [Operation("scale", {"width": 16})],
        Operation("encode", {"format": "png"}),
    )
    executor = SlowLeafExecutor(delay=0.5)
    worker = RenderWorker(CacheLayout(root / "cache"), executor=executor, busy_timeout=0.05)
    server = ZmqWorkerServer(endpoint, worker=worker, max_workers=2)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    results: dict[str, Path] = {}
    errors: dict[str, Exception] = {}

    def request(name: str) -> None:
        try:
            with ZmqWorkerClient(endpoint, timeout_ms=10_000) as worker_client:
                results[name] = ImgCacheClient(root, worker_client).get(spec)
        except Exception as error:  # pragma: no cover - assertion happens below
            errors[name] = error

    threads = [Thread(target=request, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    server._running.clear()
    server_thread.join(timeout=5)
    server.close()

    assert errors == {}
    assert results["a"] == results["b"]
    assert results["a"].exists()
    assert executor.leaf_writes == 1


def test_zmq_client_retries_busy_on_same_socket():
    import zmq

    socket = Mock()
    socket.poll.return_value = zmq.POLLIN
    socket.recv_json.side_effect = [
        {"ok": False, "error": {"type": "Busy", "message": "busy"}, "retry_after": 0.01},
        {"ok": True, "relpath": "cache/leaves/out.png"},
    ]
    context = Mock()
    context.socket.return_value = socket
    client = ZmqWorkerClient("tcp://worker:5555", context=context, request_retries=0, timeout_ms=1_000)
    spec = ImageSpec.build(
        SourceSpec("file", mime="image/x-portable-pixmap"),
        [],
        Operation("encode", {"format": "png"}),
    )

    path = client.materialize(spec)

    assert path == Path("cache/leaves/out.png")
    assert context.socket.call_count == 1  # Busy never rebuilds the REQ socket
    assert socket.send_json.call_count == 2


def test_zmq_client_busy_bounded_by_deadline():
    import zmq

    socket = Mock()
    socket.poll.return_value = zmq.POLLIN
    socket.recv_json.return_value = {
        "ok": False,
        "error": {"type": "Busy", "message": "busy"},
        "retry_after": 0.05,
    }
    context = Mock()
    context.socket.return_value = socket
    client = ZmqWorkerClient("tcp://worker:5555", context=context, request_retries=0, timeout_ms=100)
    spec = ImageSpec.build(
        SourceSpec("file", mime="image/x-portable-pixmap"),
        [],
        Operation("encode", {"format": "png"}),
    )

    with pytest.raises(TimeoutError, match="busy"):
        client.materialize(spec)


def test_zmq_client_recreates_req_socket_after_timeout():
    import zmq

    first_socket = Mock()
    first_socket.poll.return_value = 0
    second_socket = Mock()
    second_socket.poll.return_value = zmq.POLLIN
    second_socket.recv_json.return_value = {"ok": True, "relpath": "cache/leaves/out.png"}
    context = Mock()
    context.socket.side_effect = [first_socket, second_socket]
    client = ZmqWorkerClient("tcp://worker:5555", context=context, request_retries=1, timeout_ms=1)
    spec = ImageSpec.build(
        SourceSpec("file", mime="image/x-portable-pixmap"),
        [],
        Operation("encode", {"format": "png"}),
    )

    path = client.materialize(spec)

    assert path == Path("cache/leaves/out.png")
    assert first_socket.close.called
    assert context.socket.call_count == 2
