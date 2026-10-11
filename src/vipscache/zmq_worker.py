from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path
from typing import Any, Self

from vipscache.errors import BusyError, ErrorType, InvalidSpecError, error_payload
from vipscache.eviction import evict_ttl
from vipscache.layout import CacheLayout
from vipscache.limits import WorkerLimits
from vipscache.management import ManagementService
from vipscache.settings import WorkerSettings, configure_libvips
from vipscache.spec import ENGINE_VERSION, ImageSpec
from vipscache.state import WorkerState, state_versions
from vipscache.worker import RenderWorker


class ZmqWorkerServer:
    """Serve render requests and optional retention cleanup over ZeroMQ."""

    def __init__(
        self,
        endpoint: str,
        *,
        worker: RenderWorker | None = None,
        root: str | Path | None = None,
        context: Any | None = None,
        max_workers: int = 1,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        state_dir: str | Path | None = None,
        state_map_size_mb: int = 1024,
        busy_timeout_seconds: float = 2.0,
        management_dir: str | Path | None = None,
        management_session_timeout_seconds: float = 300.0,
        gc_interval_seconds: float = 60.0,
        max_cache_bytes: int | None = None,
        limits: WorkerLimits | None = None,
    ) -> None:
        if (
            max_cache_bytes is not None
            and management_dir is None
            and (worker is None or worker.management is None)
        ):
            raise ValueError(
                "max_cache_bytes requires management_dir or an injected management client"
            )
        try:
            import zmq
        except ImportError as error:
            raise RuntimeError(
                "ZmqWorkerServer requires pyzmq; install vipscache[worker]."
            ) from error

        if worker is None and root is None:
            raise ValueError("worker or root is required")

        self._zmq = zmq
        self._context = context or zmq.Context.instance()
        self.endpoint = endpoint
        self.max_workers = max_workers
        self.ttl_seconds = ttl_seconds
        self.max_cache_bytes = max_cache_bytes
        self.gc_interval_seconds = gc_interval_seconds
        stats_directory = (
            Path(state_dir)
            if state_dir is not None
            else getattr(getattr(worker, "state", None), "path", None)
        )
        if stats_directory is not None:
            stats_directory = Path(stats_directory).resolve()
            if management_dir is not None and Path(management_dir).resolve().is_relative_to(
                stats_directory
            ):
                raise ValueError("management_dir must be outside disposable LMDB state_dir")
        if worker is not None:
            self.worker = worker
        else:
            assert root is not None
            root_path = Path(root)
            state = None
            if state_dir is not None:
                probe_worker = RenderWorker(CacheLayout(root_path / "cache"))
                state = WorkerState(
                    Path(state_dir),
                    map_size_mb=state_map_size_mb,
                    versions=state_versions(
                        engine_version=ENGINE_VERSION,
                        libvips=_libvips_version(probe_worker.executor),
                    ),
                )
                self.worker = RenderWorker(
                    CacheLayout(root_path / "cache"),
                    executor=probe_worker.executor,
                    state=state,
                    limits=limits,
                    busy_timeout=busy_timeout_seconds,
                )
            else:
                self.worker = RenderWorker(
                    CacheLayout(root_path / "cache"),
                    limits=limits,
                    busy_timeout=busy_timeout_seconds,
                )
        self._management_service = None
        if self.worker.management is None and management_dir is not None:
            self._management_service = ManagementService(
                self.worker.layout,
                management_dir,
                session_timeout=management_session_timeout_seconds,
            )
            self.worker.management = self._management_service.client
        self._running = threading.Event()
        self._stopped = threading.Event()
        self._gc_thread = None
        self._socket = None
        self._frontend = None
        self._backend = None
        self._worker_threads: list[threading.Thread] = []

    def serve_forever(self) -> None:
        self._running.set()
        if self.gc_interval_seconds > 0 and self.worker.management is not None:
            self._gc_thread = threading.Thread(target=self._gc_loop, daemon=True)
            self._gc_thread.start()
        if self.max_workers == 1:
            self._serve_simple()
        else:
            self._serve_pool()

    def serve_one(self) -> None:
        if self._socket is None:
            self._bind_simple_socket()
        assert self._socket is not None
        request = self._socket.recv_json()
        response = self._handle_request(request)
        self._socket.send_json(response)

    def _serve_simple(self) -> None:
        self._bind_simple_socket()
        assert self._socket is not None
        while self._running.is_set():
            if self._socket.poll(100, self._zmq.POLLIN):
                self.serve_one()

    def _gc_loop(self) -> None:
        """Run periodic cleanup while treating startup reconciliation as temporary busy."""
        management = self.worker.management
        assert management is not None
        while not self._stopped.wait(self.gc_interval_seconds):
            try:
                management.evict(self.ttl_seconds, self.max_cache_bytes)
            except BusyError:
                pass  # Initial reconciliation has not completed yet.
            except Exception:
                logging.getLogger("vipscache.worker").exception(
                    "cache cleanup failed; no unmanaged fallback"
                )

    def _serve_pool(self) -> None:
        """Route requests between the frontend socket and render threads."""
        backend_endpoint = f"inproc://vipscache-workers-{id(self)}"
        self._frontend = self._context.socket(self._zmq.ROUTER)
        self._frontend.setsockopt(self._zmq.LINGER, 0)
        self._frontend.bind(self.endpoint)
        self._backend = self._context.socket(self._zmq.DEALER)
        self._backend.setsockopt(self._zmq.LINGER, 0)
        self._backend.bind(backend_endpoint)

        for index in range(self.max_workers):
            thread = threading.Thread(
                target=self._worker_loop, args=(backend_endpoint,), daemon=True
            )
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

    def _handle_request(
        self, request: dict[str, Any], *, allow_shutdown: bool = True
    ) -> dict[str, Any]:
        try:
            return self._dispatch(request, allow_shutdown=allow_shutdown)
        except BusyError as error:
            return {
                "ok": False,
                "error": error_payload(error),
                "retry_after": getattr(error, "retry_after", self.worker.busy_timeout),
            }
        except Exception as error:
            payload = error_payload(error)
            response = {"ok": False, "error": payload}
            return response

    def _dispatch(self, request: dict[str, Any], *, allow_shutdown: bool = True) -> dict[str, Any]:
        """Validate and execute one worker protocol request."""
        method = request.get("method")

        if method == "shutdown":
            if allow_shutdown:
                self._running.clear()
                return {"ok": True}
            return {
                "error": {
                    "message": "shutdown is only supported by the single-worker server",
                    "type": ErrorType.INVALID_SPEC,
                },
                "ok": False,
            }

        if method == "health":
            return {"ok": True}

        if method == "evict_ttl":
            if self.worker.management is None:
                report = evict_ttl(self.worker.layout, self.ttl_seconds)
            else:
                report = self.worker.management.evict(self.ttl_seconds, self.max_cache_bytes)
            return {
                "deleted_bytes": report.deleted_bytes,
                "deleted_files": report.deleted_files,
                "ok": True,
            }

        if method == "stats":
            if self.worker.state is None:
                return {
                    "error": {
                        "message": "worker state is disabled",
                        "type": ErrorType.STATE_DISABLED,
                    },
                    "ok": False,
                }
            if "key" in request:
                inspection = self.worker.state.inspect(str(request["key"]))
                return {
                    "children": inspection["children"],
                    "ok": True,
                    "record": inspection["record"],
                    "record_type": inspection["record_type"],
                }
            top = int(request.get("top", 10))
            return {"nodes": self.worker.state.top_nodes(top), "ok": True}

        if method == "identify":
            spec = self._parse_spec(request.get("spec"))
            return {"meta": self.worker.measure(spec), "ok": True}

        if method == "unpin":
            self.worker.unpin(self._parse_spec(request.get("spec")))
            return {"ok": True}
        if method == "delete":
            self.worker.delete([self._parse_spec(item) for item in self._spec_list(request)])
            return {"ok": True}
        if method not in {"materialize", "ensure"}:
            raise InvalidSpecError(f"unsupported method: {method}")
        spec = self._parse_spec(request.get("spec"))
        if method == "ensure":
            path = self.worker.ensure(spec, retain=request.get("retain", "ttl"))
        else:
            if "retain" in request:
                raise InvalidSpecError("retain requires ensure")
            path = self.worker.materialize(spec)

        try:
            relpath = Path("cache", *path.relative_to(self.worker.layout.root).parts)
        except ValueError:
            relpath = path
        return {"ok": True, "relpath": relpath.as_posix()}

    @staticmethod
    def _parse_spec(payload: Any) -> ImageSpec:
        if not isinstance(payload, dict):
            raise InvalidSpecError("spec must be an object")
        try:
            return ImageSpec.from_payload(payload)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise InvalidSpecError(str(error)) from error

    @staticmethod
    def _spec_list(request: dict[str, Any]) -> list:
        specs = request.get("specs")
        if not isinstance(specs, list):
            raise InvalidSpecError("specs must be a list")
        return specs

    def close(self) -> None:
        self._running.clear()
        self._stopped.set()
        for thread in self._worker_threads:
            thread.join()
        if self._gc_thread is not None:
            self._gc_thread.join()
        for socket in (self._socket, self._frontend, self._backend):
            if socket is not None:
                socket.close(linger=0)
        if self.worker.state is not None:
            self.worker.state.close()
        if self._management_service is not None:
            self._management_service.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    """Load worker settings, configure libvips and serve ZeroMQ requests."""
    parser = argparse.ArgumentParser(description="Run an vipscache ZeroMQ render worker.")
    parser.add_argument("--endpoint", help="ZMQ endpoint to bind, e.g. tcp://*:5555")
    parser.add_argument("--root", help="Shared vipscache root mounted into the worker")
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
        state_dir=settings.state_dir,
        state_map_size_mb=settings.state_map_size_mb,
        busy_timeout_seconds=settings.busy_timeout_seconds,
        management_dir=settings.management_dir,
        management_session_timeout_seconds=settings.management_session_timeout_seconds,
        gc_interval_seconds=settings.gc_interval_seconds,
        max_cache_bytes=settings.max_cache_bytes,
        limits=WorkerLimits(
            max_input_pixels=settings.max_input_pixels,
            max_output_pixels=settings.max_output_pixels,
            max_dpi=settings.max_dpi,
        ),
    ) as server:
        server.serve_forever()
    return 0


def _libvips_version(executor: Any) -> str:
    version = getattr(executor, "libvips_version", None)
    if version is None:
        return ""
    return str(version())


if __name__ == "__main__":
    raise SystemExit(main())
