from __future__ import annotations

import errno
import math
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from vipscache.errors import ErrorType, VipsCacheError
from vipscache.hash import file_id
from vipscache.layout import validate_key
from vipscache.spec import SourceSpec

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_STAGING = re.compile(rf"\.staging-{_UUID}")
_LEGACY_STAGING = re.compile(rf"(?:\.{_UUID}|\.[0-9a-f]{{32}}\.[a-z0-9_]{{8}})")


def validate_file_id(value: str) -> str:
    return validate_key(value)


class OriginalsStore:
    """Publish original copies or links atomically under their content identifiers."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, source_file_id: str) -> Path:
        return self.root / validate_file_id(source_file_id)

    def unlink(self, source_file_id: str) -> None:
        self.path_for(source_file_id).unlink(missing_ok=True)

    @contextmanager
    def _staging(self) -> Generator[Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / f".staging-{uuid.uuid4()}"
        directory.mkdir()
        try:
            yield directory / "source"
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def _publish(staged: Path, target: Path) -> None:
        try:
            os.replace(staged, target)
        except FileNotFoundError as error:
            raise VipsCacheError(
                ErrorType.STAGING_LOST,
                "import staging disappeared before publication; caller may retry",
            ) from error

    @staticmethod
    def _import_missing(source: Path, staged: Path, error: FileNotFoundError) -> None:
        if not staged.parent.is_dir():
            raise VipsCacheError(
                ErrorType.STAGING_LOST, "import staging disappeared while writing; caller may retry"
            ) from error
        raise VipsCacheError(ErrorType.ORIGINAL_MISSING, str(source)) from error

    def link(
        self,
        path: str | Path,
        *,
        file_id: str | None = None,
        mode: str = "hardlink",
        link_target: str | Path | None = None,
        mime: str | None = None,
        metadata: dict[str, object] | None = None,
        verify: bool = False,
    ) -> SourceSpec:
        """Stage a hardlink or symlink and publish it without copying the original."""
        if mode not in {"hardlink", "symlink"}:
            raise ValueError("mode must be hardlink or symlink")
        if link_target is not None and mode != "symlink":
            raise ValueError("link_target requires symlink mode")
        try:
            source = Path(path).resolve(strict=True)
        except FileNotFoundError as error:
            raise VipsCacheError(ErrorType.ORIGINAL_MISSING, str(path)) from error
        digest = self._digest(source, file_id, verify)
        target = self.path_for(digest)
        spec = SourceSpec(digest, mime, {"filename": Path(path).name, **(metadata or {})})
        try:
            # A dangling raw symlink must be replaced, not treated as a hit.
            with target.open("rb"):
                return spec
        except FileNotFoundError:
            pass
        with self._staging() as staged:
            try:
                if mode == "hardlink":
                    os.link(source, staged)
                else:
                    destination = Path(link_target) if link_target is not None else source
                    if not destination.is_absolute():
                        raise ValueError("link_target must be an absolute worker-visible path")
                    staged.symlink_to(destination)
            except FileNotFoundError as error:
                self._import_missing(source, staged, error)
            except OSError as error:
                if error.errno == errno.EXDEV:
                    raise VipsCacheError(
                        ErrorType.CROSS_DEVICE_LINK,
                        "hardlink crosses a filesystem or mount boundary",
                    ) from error
                raise
            self._publish(staged, target)
        return spec

    @staticmethod
    def _digest(path: Path, known: str | None, verify: bool) -> str:
        if known is not None:
            validate_file_id(known)
        if known is None or verify:
            try:
                actual = file_id(path)
            except FileNotFoundError as error:
                raise VipsCacheError(ErrorType.ORIGINAL_MISSING, str(path)) from error
            if known is not None and actual != known:
                raise VipsCacheError(
                    ErrorType.HASH_MISMATCH, "file_id does not match source content"
                )
            return actual
        return known

    def put(
        self,
        path: str | Path,
        *,
        mime: str | None = None,
        metadata: dict[str, object] | None = None,
        source_file_id: str | None = None,
        verify: bool = False,
    ) -> SourceSpec:
        source_path = Path(path)
        source_file_id = self._digest(source_path, source_file_id, verify)
        stored_path = self.path_for(source_file_id)
        if not stored_path.exists():
            with self._staging() as staged:
                try:
                    shutil.copyfile(source_path, staged)
                except FileNotFoundError as error:
                    self._import_missing(source_path, staged, error)
                self._publish(staged, stored_path)
        return SourceSpec(source_file_id, mime, {"filename": source_path.name, **(metadata or {})})

    def cleanup_staging(self, *, older_than: float = 24 * 60 * 60, now: float | None = None) -> int:
        """Remove known old staging entries; concurrent imports may get StagingLost.

        Legacy direct links use ctime as well as mtime, conservatively retaining
        recently linked old originals. New imports use their own directory clock.
        Unknown names/contents and all canonical raw entries are left alone.
        """
        if not math.isfinite(older_than) or older_than < 0:
            raise ValueError("older_than must be finite and nonnegative")
        cutoff = (time.time() if now is None else now) - older_than
        removed = 0
        if not self.root.exists():
            return removed
        for path in self.root.iterdir():
            modern = _STAGING.fullmatch(path.name) is not None
            if not modern and not _LEGACY_STAGING.fullmatch(path.name):
                continue
            try:
                entry = path.lstat()
                timestamp = (
                    entry.st_mtime
                    if stat.S_ISDIR(entry.st_mode)
                    else max(entry.st_mtime, entry.st_ctime)
                )
                if timestamp > cutoff:
                    continue
                if stat.S_ISDIR(entry.st_mode):
                    if any(child.name != "source" for child in path.iterdir()):
                        continue
                    source = path / "source"
                    if source.is_dir() and not source.is_symlink():
                        continue
                    source.unlink(missing_ok=True)
                    path.rmdir()
                elif not modern:
                    path.unlink()
                else:
                    continue
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as error:
                if error.errno != errno.ENOTEMPTY:
                    raise
        return removed
