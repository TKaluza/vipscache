from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any

from imgcache.eviction import evict_ttl
from imgcache.layout import CacheLayout
from imgcache.settings import WorkerSettings, configure_libvips
from imgcache.spec import ImageSpec
from imgcache.worker import RenderWorker


class ZmqWorkerServer:
    def __init__(
        self,
        endpoint: str,
        *,
        worker: RenderWorker | None = None,
        root: str | Path | None = None,
        context: Any | None = None,
        max_workers: int = 1,
        ttl_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        try:
            import zmq
        except ImportError as error:
            raise RuntimeError("ZmqWorkerServer requires pyzmq; install imgcache[worker].") from error

        if worker is None and root is None:
            raise ValueError("worker or root is required")

        self._zmq = zmq
        self._context = context or zmq.Context.instance()
        self.endpoint = endpoint
        self.max_workers = max_workers
        self.ttl_seconds = ttl_seconds
        if worker is not None:
            self.worker = worker
        else:
            root_path = Path(root)
            self.worker = RenderWorker(CacheLayout(root_path / "cache"))
        self._running = threading.Event()
        self._socket = None
        self._frontend = None
        self._backend = None
        self._worker_threads: list[threading.Thread] = []

    def serve_forever(self) -> None:
        self._running.set()
        if self.max_workers == 1:
            self._serve_simple()
        else:
            self._serve_pool()

    def serve_one(self) -> None:
        if self._socket is None:
            self._bind_simple_socket()
        request = self._socket.recv_json()
        response = self._handle_request(request)
        self._socket.send_json(response)

    def _serve_simple(self) -> None:
        self._bind_simple_socket()
        while self._running.is_set():
            self.serve_one()

    def _serve_pool(self) -> None:
        backend_endpoint = f"inproc://imgcache-workers-{id(self)}"
        self._frontend = self._context.socket(self._zmq.ROUTER)
        self._frontend.setsockopt(self._zmq.LINGER, 0)
        self._frontend.bind(self.endpoint)
        self._backend = self._context.socket(self._zmq.DEALER)
        self._backend.setsockopt(self._zmq.LINGER, 0)
        self._backend.bind(backend_endpoint)

        for index in range(self.max_workers):
            thread = threading.Thread(target=self._worker_loop, args=(backend_endpoint,), daemon=True)
            thread.start()
            self._worker_threads.append(thread)

        poller = self._zmq.Poller()
        poller.register(self._frontend, self._zmq.POLLIN)
        poller.register(self._backend, self._zmq.POLLIN)

        while self._running.is_set():
            events = dict(poller.poll(100))
            if self._frontend in events:
                self._backend.send_multipart(self._frontend.recv_multipart())
            if self._backend in events:
                self._frontend.send_multipart(self._backend.recv_multipart())

    def _worker_loop(self, backend_endpoint: str) -> None:
        socket = self._context.socket(self._zmq.REP)
        socket.setsockopt(self._zmq.LINGER, 0)
        socket.connect(backend_endpoint)
        try:
            while self._running.is_set():
                if not socket.poll(100, self._zmq.POLLIN):
                    continue
                request = socket.recv_json()
                socket.send_json(self._handle_request(request, allow_shutdown=False))
        finally:
            socket.close(linger=0)

    def _bind_simple_socket(self) -> None:
        if self._socket is not None:
            return
        self._socket = self._context.socket(self._zmq.REP)
        self._socket.setsockopt(self._zmq.LINGER, 0)
        self._socket.bind(self.endpoint)

    def _handle_request(self, request: dict[str, Any], *, allow_shutdown: bool = True) -> dict[str, Any]:
        method = request.get("method")

        if method == "shutdown":
            if allow_shutdown:
                self._running.clear()
                return {"ok": True}
            return {
                "error": {
                    "message": "shutdown is only supported by the single-worker server",
                    "type": "ValueError",
                },
                "ok": False,
            }

        if method == "health":
            return {"ok": True}

        if method == "evict_ttl":
            report = evict_ttl(self.worker.layout, self.ttl_seconds)
            return {
                "deleted_bytes": report.deleted_bytes,
                "deleted_files": report.deleted_files,
                "ok": True,
            }

        if method == "identify":
            try:
                spec = ImageSpec.from_payload(request["spec"])
                meta = self.worker.measure(spec)
            except Exception as error:
                return {
                    "error": {
                        "message": str(error),
                        "type": type(error).__name__,
                    },
                    "ok": False,
                }
            return {"meta": meta, "ok": True}

        if method != "materialize":
            return {
                "error": {
                    "message": f"unsupported method: {method}",
                    "type": "ValueError",
                },
                "ok": False,
            }

        try:
            spec = ImageSpec.from_payload(request["spec"])
            path = self.worker.materialize(spec)
        except Exception as error:
            return {
                "error": {
                    "message": str(error),
                    "type": type(error).__name__,
                },
                "ok": False,
            }

        try:
            relpath = Path("cache", *path.relative_to(self.worker.layout.root).parts)
        except ValueError:
            relpath = path
        return {"ok": True, "relpath": relpath.as_posix()}

    def close(self) -> None:
        self._running.clear()
        for socket in (self._socket, self._frontend, self._backend):
            if socket is not None:
                socket.close(linger=0)

    def __enter__(self) -> "ZmqWorkerServer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an imgcache ZeroMQ render worker.")
    parser.add_argument("--endpoint", help="ZMQ endpoint to bind, e.g. tcp://*:5555")
    parser.add_argument("--root", help="Shared imgcache root mounted into the worker")
    parser.add_argument("--max-workers", type=int, help="Maximum concurrent render jobs")
    parser.add_argument("--ttl-seconds", type=int, help="TTL for file-based eviction")
    args = parser.parse_args(argv)
    settings = WorkerSettings()
    if args.endpoint is not None:
        settings.endpoint = args.endpoint
    if args.root is not None:
        settings.root = Path(args.root)
    if args.max_workers is not None:
        settings.max_workers = args.max_workers
    if args.ttl_seconds is not None:
        settings.ttl_seconds = args.ttl_seconds

    configure_libvips(settings)

    with ZmqWorkerServer(
        settings.endpoint,
        root=settings.root,
        max_workers=settings.max_workers,
        ttl_seconds=settings.ttl_seconds,
    ) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
