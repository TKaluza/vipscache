from __future__ import annotations

from pathlib import Path
from threading import Thread
from unittest.mock import Mock

from imgcache import ImgCacheClient, Operation, SourceSpec
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
