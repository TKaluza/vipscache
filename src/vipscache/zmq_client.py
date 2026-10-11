from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Self

from vipscache.errors import ErrorType, RequestTimeoutError, VipsCacheError
from vipscache.spec import ImageSpec, RetainPolicy


class ZmqWorkerClient:
    """Exchange bounded worker requests with independent sync and async sockets."""

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
            raise RuntimeError(
                "ZmqWorkerClient requires pyzmq; install vipscache[client]."
            ) from error

        self._zmq = zmq
        self._context = context or zmq.Context.instance()
        self.endpoint = endpoint
        self.request_retries = request_retries
        self.timeout_ms = timeout_ms
        self._local = threading.local()

    def materialize(self, spec: ImageSpec) -> Path:
        return Path(
            self._checked(self._request({"method": "materialize", "spec": spec.to_payload()}))[
                "relpath"
            ]
        )

    async def amaterialize(self, spec: ImageSpec) -> Path:
        return Path(
            self._checked(
                await self._arequest({"method": "materialize", "spec": spec.to_payload()})
            )["relpath"]
        )

    def ensure(self, spec: ImageSpec, *, retain: RetainPolicy | str = RetainPolicy.TTL) -> Path:
        response = self._checked(
            self._request(
                {"method": "ensure", "spec": spec.to_payload(), "retain": RetainPolicy(retain)}
            )
        )
        return Path(response["relpath"])

    async def aensure(
        self, spec: ImageSpec, *, retain: RetainPolicy | str = RetainPolicy.TTL
    ) -> Path:
        response = self._checked(
            await self._arequest(
                {"method": "ensure", "spec": spec.to_payload(), "retain": RetainPolicy(retain)}
            )
        )
        return Path(response["relpath"])

    def unpin(self, spec: ImageSpec) -> None:
        self._checked(self._request({"method": "unpin", "spec": spec.to_payload()}))

    async def aunpin(self, spec: ImageSpec) -> None:
        self._checked(await self._arequest({"method": "unpin", "spec": spec.to_payload()}))

    def delete(self, specs: list[ImageSpec]) -> None:
        self._checked(
            self._request({"method": "delete", "specs": [spec.to_payload() for spec in specs]})
        )

    async def adelete(self, specs: list[ImageSpec]) -> None:
        self._checked(
            await self._arequest(
                {"method": "delete", "specs": [spec.to_payload() for spec in specs]}
            )
        )

    @staticmethod
    def _checked(response: dict[str, Any]) -> dict[str, Any]:
        if not response.get("ok"):
            error = response.get("error", {})
            raise VipsCacheError(
                error.get("type", ErrorType.WORKER_ERROR),
                error.get("message", "worker request failed"),
            )
        return response

    def identify(self, spec: ImageSpec) -> dict[str, Any]:
        return self._checked(self._request({"method": "identify", "spec": spec.to_payload()}))[
            "meta"
        ]

    async def aidentify(self, spec: ImageSpec) -> dict[str, Any]:
        return self._checked(
            await self._arequest({"method": "identify", "spec": spec.to_payload()})
        )["meta"]

    def shutdown_worker(self) -> None:
        self._checked(self._request({"method": "shutdown"}))

    def healthcheck(self) -> bool:
        return bool(self._checked(self._request({"method": "health"}))["ok"])

    def evict_ttl(self) -> dict[str, Any]:
        return self._checked(self._request({"method": "evict_ttl"}))

    def stats(self, *, top: int | None = None, key: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": "stats"}
        if key is not None:
            payload["key"] = key
        if top is not None:
            payload["top"] = top
        return self._checked(self._request(payload))

    def close(self) -> None:
        socket = getattr(self._local, "socket", None)
        if socket is not None:
            socket.close(linger=0)
            self._local.socket = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Retry transport failures and busy replies within one request deadline."""
        attempts = self.request_retries + 1
        deadline = time.monotonic() + (self.timeout_ms / 1000.0) * attempts
        for attempt in range(attempts):
            try:
                socket = self._socket()
                socket.send_json(payload)
                while socket.poll(self._poll_ms(deadline)):
                    response = socket.recv_json()
                    retry_after = _busy_retry_after(response)
                    if retry_after is None:
                        return response
                    # Busy is a regular REP reply: the REQ socket stays usable,
                    # so resend on the same socket after the advised pause.
                    if time.monotonic() + retry_after >= deadline:
                        raise RequestTimeoutError(
                            "ZMQ worker stayed busy past the request deadline"
                        )
                    time.sleep(retry_after)
                    socket.send_json(payload)
            except self._zmq.ZMQError as error:
                if attempt == attempts - 1:
                    raise VipsCacheError(
                        ErrorType.TRANSPORT_ERROR, f"ZMQ request failed: {error}"
                    ) from error

            self._reset_socket()

        raise RequestTimeoutError(f"ZMQ worker did not reply after {attempts} request attempts")

    def _poll_ms(self, deadline: float) -> int:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        return max(0, min(self.timeout_ms, remaining_ms))

    def _new_socket(self):
        socket = self._context.socket(self._zmq.REQ)
        socket.setsockopt(self._zmq.LINGER, 0)
        socket.setsockopt(self._zmq.RCVTIMEO, self.timeout_ms)
        socket.setsockopt(self._zmq.SNDTIMEO, self.timeout_ms)
        socket.connect(self.endpoint)
        return socket

    def _socket(self):
        socket = getattr(self._local, "socket", None)
        if socket is None:
            socket = self._new_socket()
            self._local.socket = socket
        return socket

    def _reset_socket(self) -> None:
        socket = getattr(self._local, "socket", None)
        if socket is not None:
            socket.close(linger=0)
        self._local.socket = self._new_socket()

    async def _arequest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Exchange a request asynchronously, closing its socket on completion or cancellation."""
        try:
            import zmq.asyncio
        except ImportError as error:
            raise RuntimeError(
                "async ZMQ requests require pyzmq; install vipscache[client]."
            ) from error

        context = zmq.asyncio.Context.instance()
        attempts = self.request_retries + 1
        deadline = time.monotonic() + (self.timeout_ms / 1000.0) * attempts
        for attempt in range(attempts):
            socket = context.socket(self._zmq.REQ)
            socket.setsockopt(self._zmq.LINGER, 0)
            socket.connect(self.endpoint)
            try:
                await socket.send_json(payload)
                while await socket.poll(self._poll_ms(deadline)):
                    response = await socket.recv_json()
                    retry_after = _busy_retry_after(response)
                    if retry_after is None:
                        return response
                    # Busy is a regular REP reply: resend on the same socket.
                    if time.monotonic() + retry_after >= deadline:
                        raise RequestTimeoutError(
                            "ZMQ worker stayed busy past the request deadline"
                        )
                    await asyncio.sleep(retry_after)
                    await socket.send_json(payload)
            except self._zmq.ZMQError as error:
                if attempt == attempts - 1:
                    raise VipsCacheError(
                        ErrorType.TRANSPORT_ERROR, f"ZMQ request failed: {error}"
                    ) from error
            finally:
                socket.close(linger=0)
            await asyncio.sleep(0)

        raise RequestTimeoutError(f"ZMQ worker did not reply after {attempts} request attempts")


def _busy_retry_after(response: dict[str, Any]) -> float | None:
    """Return the advised pause for a Busy reply, or None for any other reply."""
    if response.get("ok"):
        return None
    error = response.get("error") or {}
    if error.get("type") != ErrorType.BUSY:
        return None
    try:
        retry_after = float(response.get("retry_after", 0.5))
    except (TypeError, ValueError):
        retry_after = 0.5
    return max(retry_after, 0.0)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check an vipscache ZMQ worker.")
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    parser.add_argument("--timeout-ms", type=int, default=5_000)
    args = parser.parse_args(argv)

    with ZmqWorkerClient(args.endpoint, timeout_ms=args.timeout_ms, request_retries=0) as client:
        return 0 if client.healthcheck() else 1


if __name__ == "__main__":
    raise SystemExit(main())
