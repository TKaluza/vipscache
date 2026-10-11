"""Durable retention catalog, owned by one local management service."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from itertools import batched
from pathlib import Path
from typing import Any

from vipscache.errors import BusyError, ErrorType, InvalidSpecError, VipsCacheError
from vipscache.eviction import EvictionReport
from vipscache.layout import CacheLayout
from vipscache.locking import ArtifactLocks
from vipscache.spec import RetainPolicy

_BATCH = 128
_LOGGER = logging.getLogger(__name__)


def sync_directory(path: Path) -> None:
    """Persist directory entry changes after publication or deletion."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Catalog:
    """Journal artifact changes and reconcile retention with shared storage."""

    def __init__(self, layout: CacheLayout, directory: Path) -> None:
        self.layout = layout
        self._guard = threading.RLock()
        self._locks = ArtifactLocks()
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.scan_error: Exception | None = None
        self._scan_thread: threading.Thread | None = None
        directory.mkdir(parents=True, exist_ok=True)
        dbpath = directory / "retention.sqlite3"
        if layout.management_marker.exists() and not dbpath.exists():
            raise VipsCacheError(
                ErrorType.MANAGEMENT_UNAVAILABLE,
                "retention database is missing; restore it before enabling cleanup",
            )
        self.db = sqlite3.connect(dbpath, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize_schema()
            self._claim_cache()
            self._recover()
        except BaseException:
            self.db.close()
            raise

    def _initialize_schema(self) -> None:
        """Create the retention schema and restore the aggregate byte counter."""
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='identity'").fetchone():
            identity = self.db.execute("SELECT * FROM identity").fetchone()
            if identity is not None and identity["schema_version"] != 1:
                raise VipsCacheError(
                    ErrorType.MANAGEMENT_UNAVAILABLE, "unsupported retention schema"
                )
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS identity (id TEXT NOT NULL, schema_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (
                path TEXT PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL,
                size INTEGER NOT NULL, published REAL NOT NULL,
                retain TEXT NOT NULL CHECK(retain IN ('ttl', 'pin')),
                revision INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'live', staged TEXT
            );
            CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY CHECK(id=1), ttl_bytes INTEGER NOT NULL);
            INSERT OR IGNORE INTO usage VALUES(1,0);
            CREATE TRIGGER IF NOT EXISTS artifact_insert AFTER INSERT ON artifacts BEGIN
                UPDATE usage SET ttl_bytes=ttl_bytes + CASE WHEN NEW.retain='ttl' AND NEW.state='live' THEN NEW.size ELSE 0 END;
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_delete AFTER DELETE ON artifacts BEGIN
                UPDATE usage SET ttl_bytes=ttl_bytes - CASE WHEN OLD.retain='ttl' AND OLD.state='live' THEN OLD.size ELSE 0 END;
            END;
            CREATE TRIGGER IF NOT EXISTS artifact_update AFTER UPDATE ON artifacts BEGIN
                UPDATE usage SET ttl_bytes=ttl_bytes
                    - CASE WHEN OLD.retain='ttl' AND OLD.state='live' THEN OLD.size ELSE 0 END
                    + CASE WHEN NEW.retain='ttl' AND NEW.state='live' THEN NEW.size ELSE 0 END;
            END;
            UPDATE usage SET ttl_bytes=(SELECT COALESCE(SUM(size),0) FROM artifacts WHERE retain='ttl' AND state='live');
        """)
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS gc_live ON artifacts(published,path) WHERE retain='ttl' AND state='live';
            DROP INDEX IF EXISTS expiry;
            CREATE TABLE IF NOT EXISTS staging (path TEXT PRIMARY KEY, key TEXT NOT NULL);
        """)

    def _claim_cache(self) -> None:
        """Match the catalog identity to an atomically published cache marker."""
        identity = self.db.execute("SELECT * FROM identity").fetchone()
        if identity is None:
            identity = {"id": uuid.uuid4().hex}
            with self.db:
                self.db.execute("INSERT INTO identity VALUES (?,1)", (identity["id"],))
        marker = self.layout.management_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            # Publish a complete marker without overwriting another owner's claim.
            fd, name = tempfile.mkstemp(prefix=".managed-", dir=marker.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump({"id": identity["id"], "schema": 1}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, marker)
                except FileExistsError:
                    pass
                sync_directory(marker.parent)
            finally:
                temporary.unlink(missing_ok=True)
        try:
            matched = json.loads(marker.read_text())["id"] == identity["id"]
        except (ValueError, KeyError) as error:
            raise VipsCacheError(
                ErrorType.MANAGEMENT_UNAVAILABLE,
                "invalid retention marker; restore it before cleanup",
            ) from error
        if not matched:
            raise VipsCacheError(
                ErrorType.MANAGEMENT_UNAVAILABLE, "cache belongs to a different retention database"
            )

    def artifact(self, rel: str) -> tuple[Path, str, str]:
        return self.layout.artifact(rel)

    @contextmanager
    def locked(
        self, keys: list[str], timeout: float = 2.0, *, read: Iterable[str] = ()
    ) -> Generator[None]:
        """Acquire all artifact locks in a consistent order within one deadline."""
        with self._locks.hold(keys, timeout, read=read):
            yield

    def start_reconcile(self) -> None:
        """Start the initial filesystem scan without blocking render requests."""
        if self._scan_thread is None:
            self._scan_thread = threading.Thread(
                target=self._reconcile, name="vipscache-reconcile", daemon=True
            )
            self._scan_thread.start()

    def wait_ready(self, timeout: float = 2.0) -> None:
        """Allow cleanup only after successful initial reconciliation."""
        if not self.ready.wait(timeout):
            raise BusyError("retention reconciliation is still running")
        if self.scan_error is not None or self.stopping.is_set():
            raise VipsCacheError(
                ErrorType.MANAGEMENT_UNAVAILABLE,
                "retention reconciliation failed or service is stopping",
            ) from self.scan_error

    def _reconcile(self) -> None:
        """Import files and remove stale rows in bounded transactions."""
        try:
            # Files -> rows, then rows -> files. Recheck under the same artifact
            # lock used by publication/deletion; directory listings are only hints.
            for entries in batched(self._disk_artifacts(), _BATCH):
                self._reconcile_batch(entries)
            cursor = ""
            while not self.stopping.is_set():
                with self._guard:
                    rows = self.db.execute(
                        "SELECT path,key FROM artifacts WHERE path>? ORDER BY path LIMIT ?",
                        (cursor, _BATCH),
                    ).fetchall()
                if not rows:
                    break
                self._reconcile_batch([(row["path"], row["key"]) for row in rows])
                cursor = rows[-1]["path"]
        except Exception as error:
            self.scan_error = error
            _LOGGER.exception("cache reconciliation failed; cleanup remains disabled")
        finally:
            self.ready.set()

    def _disk_artifacts(self) -> Iterator[tuple[str, str]]:
        """Yield recognized artifacts and discard abandoned cache staging files."""
        for base in ("nodes", "pinned", "leaves"):
            for path in (self.layout.root / base).glob("*/*"):
                if self.stopping.is_set():
                    return
                rel = path.relative_to(self.layout.root).as_posix()
                try:
                    _, key, _ = self.artifact(rel)
                except ValueError:
                    key = self.layout.staging_key(path)
                    if key is not None:
                        self._remove_orphan(path, key)
                    continue
                yield rel, key

    def _reconcile_batch(self, entries: Iterable[tuple[str, str]]) -> None:
        """Recheck a small batch under shared locks and commit only changes."""
        entries = tuple(entries)
        while not self.stopping.is_set():
            try:
                with (
                    self.locked([], timeout=0.1, read=[key for _, key in entries]),
                    self._guard,
                    self.db,
                ):
                    for rel, _ in entries:
                        path, _, _ = self.artifact(rel)
                        try:
                            self._record(
                                rel,
                                RetainPolicy.PIN
                                if self.layout.is_pinned(path)
                                else RetainPolicy.TTL,
                            )
                        except FileNotFoundError:
                            self._mark_missing(rel)
                return
            except BusyError:
                continue

    def _record(self, rel: str, retain: RetainPolicy = RetainPolicy.TTL) -> None:
        """Refresh changed metadata without renewing TTL; caller owns the transaction."""
        path, key, kind = self.artifact(rel)
        stat = path.stat()
        row = self.db.execute("SELECT * FROM artifacts WHERE path=?", (rel,)).fetchone()
        if row is not None:
            retain = RetainPolicy.PIN if row["retain"] == RetainPolicy.PIN else retain
            if (row["size"], row["retain"], row["state"], row["staged"]) == (
                stat.st_size,
                retain,
                "live",
                None,
            ):
                return
        self.db.execute(
            """INSERT INTO artifacts(path,key,kind,size,published,retain)
                VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                size=excluded.size, retain=CASE WHEN artifacts.retain='pin' THEN 'pin' ELSE excluded.retain END,
                state='live', staged=NULL, revision=artifacts.revision+1""",
            (rel, key, kind, stat.st_size, stat.st_mtime, retain),
        )

    def _mark_missing(self, rel: str) -> None:
        """Drop missing TTL rows and preserve pin intent; caller owns the transaction."""
        row = self.db.execute(
            "SELECT retain,state,size,staged FROM artifacts WHERE path=?", (rel,)
        ).fetchone()
        if row is None:
            return
        if row["retain"] == RetainPolicy.TTL:
            self.db.execute("DELETE FROM artifacts WHERE path=?", (rel,))
        elif (row["state"], row["size"], row["staged"]) != ("missing", 0, None):
            self.db.execute(
                "UPDATE artifacts SET state='missing', size=0, staged=NULL, revision=revision+1 WHERE path=?",
                (rel,),
            )

    def _recover(self) -> None:
        """Complete journaled publications and deletions before accepting requests."""
        for row in self.db.execute(
            "SELECT * FROM artifacts WHERE state IN ('deleting','publishing')"
        ).fetchall():
            path, _, _ = self.artifact(row["path"])
            if row["state"] == "deleting":
                self._delete(row["path"])
            else:
                staged = self.layout.root / row["staged"]
                self._validate_staged(path, staged)
                if staged.exists():
                    os.replace(staged, path)
                    sync_directory(path.parent)
                with self.db:
                    if path.exists():
                        self._record(row["path"], RetainPolicy(row["retain"]))
                    else:
                        self._mark_missing(row["path"])
        self.db.commit()

    def command(self, request: dict[str, Any], keys: set[str]) -> dict[str, Any]:
        """Apply a session command only to an exclusively locked artifact."""
        rel = request["path"]
        path, key, _ = self.artifact(rel)
        if key not in keys:
            raise InvalidSpecError("artifact must be exclusively locked by this session")
        try:
            retain = RetainPolicy(request.get("retain", RetainPolicy.TTL))
        except ValueError as error:
            raise InvalidSpecError("invalid retention") from error
        with self._guard:
            if self.stopping.is_set():
                raise VipsCacheError(
                    ErrorType.MANAGEMENT_UNAVAILABLE, "management service is stopping"
                )
            method = request["method"]
            if method == "stage":
                staged = self.layout.root / request["staged"]
                self._validate_staged(path, staged)
                with self.db:
                    self.db.execute(
                        "INSERT OR IGNORE INTO staging VALUES(?,?)", (request["staged"], key)
                    )
            elif method == "publish":
                self._publish(rel, self.layout.root / request["staged"], retain)
            elif method == "record":
                with self.db:
                    self._record(rel, retain)
            elif method == "unpin":
                with self.db:
                    try:
                        self._record(rel)
                    except FileNotFoundError:
                        self._mark_missing(rel)
                    self.db.execute(
                        "UPDATE artifacts SET retain='ttl', published=?, revision=revision+1 WHERE path=? AND retain='pin'",
                        (time.time(), rel),
                    )
            elif method == "delete":
                self._delete(rel)
            else:
                raise InvalidSpecError("unsupported management command")
        return {"ok": True}

    def _validate_staged(self, path: Path, staged: Path) -> None:
        if staged.parent != path.parent or self.layout.staging_key(staged) != path.stem:
            raise InvalidSpecError("invalid staging path")

    def _publish(self, rel: str, staged: Path, retain: RetainPolicy) -> None:
        """Journal intent before atomically publishing a fully written artifact."""
        path, key, kind = self.artifact(rel)
        self._validate_staged(path, staged)
        size = staged.stat().st_size
        staged_rel = staged.relative_to(self.layout.root).as_posix()
        with self.db:
            self.db.execute(
                """INSERT INTO artifacts(path,key,kind,size,published,retain,state,staged)
                VALUES (?,?,?,?,?,?,'publishing',?) ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,published=excluded.published,
                retain=CASE WHEN artifacts.retain='pin' THEN 'pin' ELSE excluded.retain END,
                state='publishing', staged=excluded.staged, revision=artifacts.revision+1""",
                (rel, key, kind, size, time.time(), retain, staged_rel),
            )
        os.replace(staged, path)
        sync_directory(path.parent)
        with self.db:
            self.db.execute("UPDATE artifacts SET state='live', staged=NULL WHERE path=?", (rel,))
            self.db.execute("DELETE FROM staging WHERE path=?", (staged_rel,))

    def _delete(self, rel: str) -> int:
        """Journal deletion before unlinking and return the removed file size."""
        path, _, _ = self.artifact(rel)
        with self.db:
            if (
                path.exists()
                and self.db.execute("SELECT 1 FROM artifacts WHERE path=?", (rel,)).fetchone()
                is None
            ):
                self._record(rel)
            self.db.execute(
                "UPDATE artifacts SET state='deleting', revision=revision+1 WHERE path=?", (rel,)
            )
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        path.unlink(missing_ok=True)
        if path.parent.exists():
            sync_directory(path.parent)
        with self.db:
            self.db.execute("DELETE FROM artifacts WHERE path=?", (rel,))
        return size

    def _remove_orphan(self, path: Path, key: str) -> None:
        try:
            with self.locked([key], timeout=0), self._guard:
                path.unlink(missing_ok=True)
                with self.db:
                    self.db.execute(
                        "DELETE FROM staging WHERE path=?",
                        (path.relative_to(self.layout.root).as_posix(),),
                    )
        except BusyError:
            pass

    def _cleanup_staging(self) -> None:
        cursor = ""
        while not self.stopping.is_set():
            with self._guard:
                rows = self.db.execute(
                    "SELECT path,key FROM staging WHERE path>? ORDER BY path LIMIT ?",
                    (cursor, _BATCH),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                cursor = row["path"]
                self._remove_orphan(self.layout.root / cursor, row["key"])

    def evict(
        self, ttl_seconds: int, max_bytes: int | None = None, now: float | None = None
    ) -> EvictionReport:
        """Remove expired artifacts, then enforce the soft budget without waiting on renders."""
        self.wait_ready()
        self._cleanup_staging()
        cutoff = (time.time() if now is None else now) - ttl_seconds
        files = size = 0
        # Expiry is independent of budget. A renewed row in an old snapshot must
        # never cause an early exit that skips other expired rows.
        for budget_pass in (False, True):
            if budget_pass and max_bytes is None:
                break
            cursor = (-float("inf"), "")
            while not self.stopping.is_set():
                with self._guard:
                    if budget_pass and max_bytes is not None and self._ttl_bytes() <= max_bytes:
                        break
                    rows = self.db.execute(
                        """SELECT path,key,published FROM artifacts
                        WHERE retain='ttl' AND state='live' AND (published,path)>(?,?) AND published<=?
                        ORDER BY published,path LIMIT ?""",
                        (*cursor, float("inf") if budget_pass else cutoff, _BATCH),
                    ).fetchall()
                if not rows:
                    break
                for candidate in rows:
                    cursor = (candidate["published"], candidate["path"])
                    try:
                        with self.locked([candidate["key"]], timeout=0), self._guard:
                            if (
                                budget_pass
                                and max_bytes is not None
                                and self._ttl_bytes() <= max_bytes
                            ):
                                break
                            row = self.db.execute(
                                "SELECT * FROM artifacts WHERE path=?", (candidate["path"],)
                            ).fetchone()
                            if (
                                row is None
                                or row["retain"] != RetainPolicy.TTL
                                or row["state"] != "live"
                            ):
                                continue
                            if not budget_pass and row["published"] > cutoff:
                                continue
                            present = (self.layout.root / row["path"]).exists()
                            size += self._delete(row["path"])
                            files += int(present)
                    except BusyError:
                        continue
        return EvictionReport(files, size)

    def _ttl_bytes(self) -> int:
        return self.db.execute("SELECT ttl_bytes FROM usage WHERE id=1").fetchone()[0]

    def close(self) -> None:
        self.stopping.set()
        if self._scan_thread is not None:
            self._scan_thread.join()
        self.db.close()
