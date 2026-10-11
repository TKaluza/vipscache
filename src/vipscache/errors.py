from __future__ import annotations

from enum import StrEnum
from typing import Any, cast


class ErrorType(StrEnum):
    ORIGINAL_MISSING = "OriginalMissing"
    UNREADABLE_FORMAT = "UnreadableFormat"
    LIMIT_EXCEEDED = "LimitExceeded"
    INVALID_SPEC = "InvalidSpec"
    TIMEOUT = "Timeout"
    BUSY = "Busy"
    WORKER_ERROR = "WorkerError"
    MANAGEMENT_UNAVAILABLE = "ManagementUnavailable"
    MANAGEMENT_REQUIRED = "ManagementRequired"
    UNSUPPORTED_CAPABILITY = "UnsupportedCapability"
    TRANSPORT_ERROR = "TransportError"
    PROTOCOL_ERROR = "ProtocolError"
    CROSS_DEVICE_LINK = "CrossDeviceLink"
    HASH_MISMATCH = "HashMismatch"
    STAGING_LOST = "StagingLost"
    STATE_DISABLED = "StateDisabled"


class VipsCacheError(RuntimeError):
    """Stable category shared by Python, Node and the JSON protocol."""

    def __init__(self, type: ErrorType | str, message: str) -> None:
        try:
            self.type = ErrorType(type)
        except ValueError:
            # Preserve categories from older or newer peers.
            self.type = type
        self.message = message
        super().__init__(f"{type}: {message}")

    def __reduce__(self):
        return (_restore_error, (self.__class__, self.__dict__))


def _restore_error(cls: type[VipsCacheError], attributes: dict[str, Any]) -> VipsCacheError:
    """Restore subclasses without invoking their incompatible constructors."""
    error = (
        cast(VipsCacheError, OSError.__new__(cls)) if issubclass(cls, OSError) else cls.__new__(cls)
    )
    VipsCacheError.__init__(error, attributes["type"], attributes["message"])
    error.__dict__.update(attributes)
    return error


class InvalidSpecError(VipsCacheError, ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorType.INVALID_SPEC, message)


class BusyError(VipsCacheError):
    def __init__(self, message: str = "artifact is in use") -> None:
        super().__init__(ErrorType.BUSY, message)


class LimitExceededError(VipsCacheError, ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorType.LIMIT_EXCEEDED, message)


class RequestTimeoutError(VipsCacheError, TimeoutError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorType.TIMEOUT, message)


def error_payload(error: Exception) -> dict[str, str]:
    if isinstance(error, VipsCacheError):
        return {"type": error.type, "message": error.message}
    return {"type": ErrorType.WORKER_ERROR, "message": str(error)}
