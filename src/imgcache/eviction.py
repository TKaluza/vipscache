from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from imgcache.layout import CacheLayout


@dataclass(frozen=True)
class EvictionReport:
    deleted_files: int = 0
    deleted_bytes: int = 0


def evict_ttl(layout: CacheLayout, ttl_seconds: int, *, now: float | None = None) -> EvictionReport:
    cutoff = (time.time() if now is None else now) - ttl_seconds
    deleted_files = 0
    deleted_bytes = 0

    for base in (layout.root / "nodes", layout.root / "leaves"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            if stat.st_mtime > cutoff:
                continue
            size = stat.st_size
            path.unlink()
            deleted_files += 1
            deleted_bytes += size

    _remove_empty_dirs(layout.root / "nodes")
    _remove_empty_dirs(layout.root / "leaves")
    return EvictionReport(deleted_files=deleted_files, deleted_bytes=deleted_bytes)


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
