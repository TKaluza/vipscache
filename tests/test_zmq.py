from __future__ import annotations

from pathlib import Path
from threading import Thread
from unittest.mock import Mock

from imgcache import CacheLayout, MaterializePolicy, Operation, SourceSpec, ThinClient
from imgcache.spec import DerivativeSpec
from imgcache.zmq_client import ZmqWorkerClient
from imgcache.zmq_worker import ZmqWorkerServer


def make_image(path: Path) -> None:
    width = 32
    height = 24
    pixels = bytearray()
    for _ in range(width * height):
        pixels.extend((0, 128, 255))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)


def test_thin_client_materializes_miss_over_zmq(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    layout = CacheLayout(tmp_path / "cache")
    source = SourceSpec.from_file(str(source_path), mime="image/x-portable-pixmap")
    spec = DerivativeSpec.build(
        source,
        [Operation("render", {"width": 16}, MaterializePolicy.FORCE)],
        Operation("encode", {"format": "png"}),
    )
    endpoint = f"ipc://{tmp_path / 'worker.sock'}"
    server = ZmqWorkerServer(endpoint, cache_root=layout.root)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with ZmqWorkerClient(endpoint) as worker_client:
        client = ThinClient(layout, worker_client)
        path = client.get(spec)
        worker_client.shutdown_worker()

    thread.join(timeout=5)
    server.close()

    assert path.exists()
    assert layout.node_path(spec.nodes[0].key).exists()


def test_zmq_worker_healthcheck(tmp_path):
    endpoint = f"ipc://{tmp_path / 'health.sock'}"
    server = ZmqWorkerServer(endpoint, cache_root=tmp_path / "cache")
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
    second_socket.recv_json.return_value = {"ok": True, "path": "/tmp/out.png"}
    context = Mock()
    context.socket.side_effect = [first_socket, second_socket]
    client = ZmqWorkerClient("tcp://worker:5555", context=context, request_retries=1, timeout_ms=1)
    spec = DerivativeSpec.build(
        SourceSpec("file", "/tmp/source.ppm", mime="image/x-portable-pixmap"),
        [],
        Operation("encode", {"format": "png"}),
    )

    path = client.materialize(spec)

    assert path == Path("/tmp/out.png")
    assert first_socket.close.called
    assert context.socket.call_count == 2
