"""Local transport for the single owner of durable cache retention."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import socketserver
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from io import BufferedIOBase
from pathlib import Path
from typing import Any, Self

from vipscache.catalog import Catalog
from vipscache.errors import (
    BusyError,
    ErrorType,
    InvalidSpecError,
    VipsCacheError,
    error_payload,
)
from vipscache.eviction import EvictionReport
from vipscache.layout import CacheLayout
from vipscache.spec import RetainPolicy

_MAX_MESSAGE = 4 * 1024 * 1024


class _Handler(socketserver.StreamRequestHandler):
    """Serve one bounded connection, holding artifact locks until session end."""

    server: _Server

    def setup(self) -> None:
        self.request.settimeout(self.server.session_timeout)
        super().setup()

    def handle(self) -> None:
        """Serve health, cleanup or a session that holds the requested artifact locks."""
        catalog = self.server.catalog
        try:
            request = self._read()
            if request is None:
                return
            method = request.get("method")
            if method == "health":
                self._reply(
                    {
                        "ok": True,
                        "root": str(catalog.layout.root.resolve()),
                        "ready": catalog.ready.is_set() and catalog.scan_error is None,
                    }
                )
            elif method == "evict":
                report = catalog.evict(
                    request["ttl_seconds"], request.get("max_bytes"), request.get("now")
                )
                self._reply({"ok": True, **report.__dict__})
            elif method == "session":
                keys = set(request["keys"])
                read = list(request.get("read", []))
                timeout = max(
                    0, min(float(request.get("timeout", 2.0)), self.server.session_timeout)
                )
                with catalog.locked(list(keys), timeout, read=read):
                    self._reply({"ok": True})
                    while True:
                        request = self._read()
                        if request is None:
                            return
                        if request.get("method") == "finish":
                            break
                        self._reply(catalog.command(request, keys))
                self._reply({"ok": True})
            else:
                raise InvalidSpecError("unsupported management method")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:
            try:
                self._reply({"ok": False, "error": error_payload(error)})
            except OSError:
                pass

    def _read(self) -> dict[str, Any] | None:
        line = self.rfile.readline(_MAX_MESSAGE + 1)
        if len(line) > _MAX_MESSAGE:
            raise InvalidSpecError("management message too large")
        if not line:
            return None
        try:
            value = json.loads(line)
        except ValueError as error:
            raise InvalidSpecError("invalid management JSON") from error
        if not isinstance(value, dict):
            raise InvalidSpecError("management request must be an object")
        return value

    def _reply(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload).encode() + b"\n")
        self.wfile.flush()


class _Server(socketserver.ThreadingUnixStreamServer):
    """Track active connections so shutdown can interrupt idle sessions."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, path: str, catalog: Catalog, session_timeout: float) -> None:
        self.catalog = catalog
        self.session_timeout = session_timeout
        self._condition = threading.Condition()
        self._connections: set[socket.socket] = set()
        super().__init__(path, _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._condition:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._finished(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finished(request)

    def _finished(self, request: socket.socket) -> None:
        with self._condition:
            self._connections.discard(request)
            self._condition.notify_all()

    def abort_sessions(self) -> None:
        with self._condition:
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def wait_drained(self) -> None:
        with self._condition:
            self._condition.wait_for(lambda: not self._connections)


class ManagementService:
    """Start or attach to an explicitly configured local retention owner."""

    def __init__(
        self,
        layout: CacheLayout,
        directory: str | Path,
        *,
        session_timeout: float = 300.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        if session_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("management timeouts must be positive")
        directory = Path(directory).resolve()
        shared_root = layout.root.parent if layout.root.name == "cache" else layout.root
        if directory.is_relative_to(shared_root.resolve()):
            raise ValueError("management_dir must be local and outside the shared cache")
        directory.mkdir(parents=True, exist_ok=True)
        self.shutdown_timeout = shutdown_timeout
        self._owner_file = (directory / "owner.lock").open("a+")
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.catalog: Catalog | None = None
        self._closed = False
        try:
            self.client = ManagementClient(self._socket_path(directory))
            if not self._claim_owner(layout):
                return
            self.catalog = Catalog(layout, directory)
            self.client.path.unlink(missing_ok=True)
            self._server = _Server(str(self.client.path), self.catalog, session_timeout)
            os.chmod(self.client.path, 0o600)
            self._thread = threading.Thread(
                target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            self._thread.start()
            self.catalog.start_reconcile()
        except BaseException:
            if self._server is not None:
                self._server.server_close()
            if self.catalog is not None:
                self.catalog.close()
            self._owner_file.close()
            raise

    @staticmethod
    def _socket_path(directory: Path) -> Path:
        runtime = Path(tempfile.gettempdir()) / f"vipscache-{os.getuid()}"
        runtime.mkdir(mode=0o700, exist_ok=True)
        if runtime.stat().st_uid != os.getuid() or runtime.stat().st_mode & 0o077:
            raise ValueError("management socket directory must be private to this user")
        digest = hashlib.sha256(str(directory).encode()).hexdigest()[:32]
        return runtime / f"{digest}.sock"

    def _claim_owner(self, layout: CacheLayout) -> bool:
        try:
            fcntl.flock(self._owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            deadline = time.monotonic() + 30
            while True:
                try:
                    health = self.client.request({"method": "health"})
                    if health["root"] != str(layout.root.resolve()):
                        raise ValueError("management directory belongs to a different cache")
                    return False
                except VipsCacheError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)

    def close(self) -> None:
        """Interrupt sessions promptly and finish storage cleanup within the close budget."""
        if self._closed:
            return
        self._closed = True
        if self._server is None:
            self._owner_file.close()
            return
        assert self.catalog is not None and self._thread is not None
        self.catalog.stopping.set()
        self._server.shutdown()
        self._server.abort_sessions()
        self._server.server_close()
        self._thread.join()
        self.client.path.unlink(missing_ok=True)
        # Socket reads are interrupted immediately. If a filesystem call itself
        # stalls, retain SQLite and the owner lock until its handler has finished.
        finalizer = threading.Thread(target=self._finalize, name="vipscache-close", daemon=True)
        finalizer.start()
        finalizer.join(self.shutdown_timeout)

    def _finalize(self) -> None:
        """Keep the owner lock until handlers and reconciliation have stopped."""
        assert self._server is not None and self.catalog is not None
        self._server.wait_drained()
        self.catalog.close()
        self._owner_file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class ManagementClient:
    """Send bounded requests to the local retention owner."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        if timeout <= 0:
            raise ValueError("management timeout must be positive")
        self.path = Path(path)
        self.timeout = timeout

    @contextmanager
    def connection(self, *, timeout: float | None = None) -> Generator[ManagementSession]:
        """Open a timed connection and preserve the original error during cleanup."""
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(self.timeout if timeout is None else timeout)
            try:
                sock.connect(str(self.path))
            except OSError as error:
                raise VipsCacheError(ErrorType.MANAGEMENT_UNAVAILABLE, str(error)) from error
            stream = sock.makefile("rwb")
            try:
                yield ManagementSession(stream)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass  # Do not replace a typed transport error with a close/flush error.

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connection() as session:
            return session.request(payload)

    @contextmanager
    def session(
        self, keys: list[str], timeout: float = 2.0, *, read: Iterable[str] = ()
    ) -> Generator[ManagementSession]:
        """Hold all requested locks until the owner acknowledges session completion."""
        with self.connection(timeout=max(self.timeout, timeout + 0.1)) as session:
            session.request(
                {"method": "session", "keys": keys, "read": list(read), "timeout": timeout}
            )
            yield session
            session.request({"method": "finish"})

    def evict(
        self, ttl_seconds: int, max_bytes: int | None = None, *, now: float | None = None
    ) -> EvictionReport:
        response = self.request(
            {"method": "evict", "ttl_seconds": ttl_seconds, "max_bytes": max_bytes, "now": now}
        )
        return EvictionReport(response["deleted_files"], response["deleted_bytes"])


class ManagementSession:
    """Publish and manage artifacts over an already locked owner connection."""

    def __init__(self, stream: BufferedIOBase) -> None:
        self.stream = stream

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Exchange one JSON command and preserve the owner's stable error category."""
        try:
            self.stream.write(json.dumps(payload).encode() + b"\n")
            self.stream.flush()
            line = self.stream.readline(_MAX_MESSAGE + 1)
            if len(line) > _MAX_MESSAGE:
                raise ValueError("management reply too large")
            response = json.loads(line)
            if not isinstance(response, dict):
                raise ValueError("invalid management reply")
        except (OSError, ValueError) as error:
            raise VipsCacheError(
                ErrorType.MANAGEMENT_UNAVAILABLE, "management connection lost or timed out"
            ) from error
        if not response.get("ok"):
            error = response.get("error", {})
            if error.get("type") == ErrorType.BUSY:
                raise BusyError(error.get("message", "artifact is in use"))
            raise VipsCacheError(
                error.get("type", ErrorType.MANAGEMENT_UNAVAILABLE),
                error.get("message", "management failed"),
            )
        return response

    def record(self, path: str, retain: RetainPolicy = RetainPolicy.TTL) -> None:
        self.request({"method": "record", "path": path, "retain": retain})

    def publish(
        self,
        layout: CacheLayout,
        path: Path,
        writer: Callable[[Path], None],
        retain: RetainPolicy = RetainPolicy.TTL,
    ) -> None:
        """Register staging, write and sync content, then ask the owner to publish."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=path.suffix, dir=path.parent)
        os.close(fd)
        temporary = Path(name)
        payload = {
            "path": path.relative_to(layout.root).as_posix(),
            "staged": temporary.relative_to(layout.root).as_posix(),
        }
        try:
            self.request({"method": "stage", **payload})
            writer(temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            self.request({"method": "publish", "retain": retain, **payload})
        finally:
            temporary.unlink(missing_ok=True)
