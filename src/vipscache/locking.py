"""Sorted shared/exclusive artifact locks; no upgrades while locks are held."""

from __future__ import annotations

import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition

from vipscache.errors import BusyError
from vipscache.layout import validate_key


@dataclass
class _Entry:
    readers: int = 0
    writer: bool = False
    writers_waiting: int = 0
    references: int = 0


class ArtifactLocks:
    """Coordinate shared readers and exclusive writers without lock upgrades."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._entries: dict[str, _Entry] = {}

    @contextmanager
    def hold(
        self, write: list[str], timeout: float = 2.0, *, read: Iterable[str] = ()
    ) -> Generator[None]:
        """Acquire sorted keys under one deadline and release every lock on exit."""
        exclusive = set(write)
        keys = sorted(exclusive | set(read))
        for key in keys:
            validate_key(key)
        deadline = time.monotonic() + max(0, timeout)
        acquired: list[tuple[str, _Entry, bool]] = []
        try:
            for key in keys:
                writing = key in exclusive
                with self._condition:
                    entry = self._entries.setdefault(key, _Entry())
                    entry.references += 1
                    entry.writers_waiting += int(writing)
                    try:
                        while entry.writer or (entry.readers if writing else entry.writers_waiting):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise BusyError()
                            self._condition.wait(remaining)
                        if writing:
                            entry.writer = True
                        else:
                            entry.readers += 1
                        acquired.append((key, entry, writing))
                    except BaseException:
                        self._drop(key, entry)
                        raise
                    finally:
                        entry.writers_waiting -= int(writing)
                        self._condition.notify_all()
            yield
        finally:
            with self._condition:
                for key, entry, writing in reversed(acquired):
                    if writing:
                        entry.writer = False
                    else:
                        entry.readers -= 1
                    self._drop(key, entry)
                self._condition.notify_all()

    def _drop(self, key: str, entry: _Entry) -> None:
        entry.references -= 1
        if not entry.references:
            del self._entries[key]
