from __future__ import annotations

from pathlib import Path
from typing import Any

from imgcache.spec import DerivativeSpec


class ZmqWorkerClient:
    def __init__(
        self,
        endpoint: str,
        *,
        request_retries: int = 2,
        timeout_ms: int = 300_000,
        context: Any | None = None,
    ) -> None:
        try:
            import zmq
        except ImportError as error:
            raise RuntimeError("ZmqWorkerClient requires pyzmq; install imgcache[client].") from error

        self._zmq = zmq
        self._context = context or zmq.Context.instance()
        self.endpoint = endpoint
        self.request_retries = request_retries
        self.timeout_ms = timeout_ms
        self._socket = self._new_socket()

    def materialize(self, spec: DerivativeSpec) -> Path:
        response = self._request({"method": "materialize", "spec": spec.to_payload()})
        if response.get("ok"):
            return Path(response["path"])

        error = response.get("error", {})
        error_type = error.get("type", "WorkerError")
        message = error.get("message", "worker request failed")
        raise RuntimeError(f"{error_type}: {message}")

    def shutdown_worker(self) -> None:
        response = self._request({"method": "shutdown"})
        if not response.get("ok"):
            raise RuntimeError("worker shutdown failed")

    def healthcheck(self) -> bool:
        response = self._request({"method": "health"})
        return bool(response.get("ok"))

    def evict_ttl(self) -> dict[str, Any]:
        response = self._request({"method": "evict_ttl"})
        if response.get("ok"):
            return response

        error = response.get("error", {})
        error_type = error.get("type", "WorkerError")
        message = error.get("message", "worker request failed")
        raise RuntimeError(f"{error_type}: {message}")

    def close(self) -> None:
        self._socket.close(linger=0)

    def __enter__(self) -> "ZmqWorkerClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = self.request_retries + 1
        for attempt in range(attempts):
            try:
                self._socket.send_json(payload)
                if self._socket.poll(self.timeout_ms, self._zmq.POLLIN):
                    return self._socket.recv_json()
            except self._zmq.ZMQError as error:
                if attempt == attempts - 1:
                    raise RuntimeError(f"ZMQ request failed: {error}") from error

            self._reset_socket()

        raise TimeoutError(f"ZMQ worker did not reply after {attempts} request attempts")

    def _new_socket(self):
        socket = self._context.socket(self._zmq.REQ)
        socket.setsockopt(self._zmq.LINGER, 0)
        socket.setsockopt(self._zmq.RCVTIMEO, self.timeout_ms)
        socket.setsockopt(self._zmq.SNDTIMEO, self.timeout_ms)
        socket.connect(self.endpoint)
        return socket

    def _reset_socket(self) -> None:
        self._socket.close(linger=0)
        self._socket = self._new_socket()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check an imgcache ZMQ worker.")
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    parser.add_argument("--timeout-ms", type=int, default=5_000)
    args = parser.parse_args(argv)

    with ZmqWorkerClient(args.endpoint, timeout_ms=args.timeout_ms, request_retries=0) as client:
        return 0 if client.healthcheck() else 1


if __name__ == "__main__":
    raise SystemExit(main())
