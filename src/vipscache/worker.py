from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any

from vipscache.errors import BusyError, ErrorType, InvalidSpecError, VipsCacheError
from vipscache.executor import ImageExecutor, VipsExecutor
from vipscache.io import atomic_write
from vipscache.layout import CacheLayout
from vipscache.limits import WorkerLimits
from vipscache.management import ManagementClient, ManagementSession
from vipscache.originals import validate_file_id
from vipscache.spec import (
    ImageSpec,
    MaterializePolicy,
    NodeSpec,
    RetainPolicy,
    SourceSpec,
)
from vipscache.state import WorkerState

_LOGGER = logging.getLogger("vipscache.worker")


class WorkerBusyError(BusyError):
    """Another render for the same leaf key is in flight; retry after `retry_after` seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"render for this key is already in progress; retry after {retry_after}s")
        self.retry_after = retry_after


class _KeyedLocks:
    """Per-key locks with refcounting so entries are removed once unused."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[str, tuple[Lock, int]] = {}

    def acquire(self, key: str, timeout: float) -> bool:
        with self._guard:
            lock, refs = self._entries.get(key, (Lock(), 0))
            self._entries[key] = (lock, refs + 1)
        if lock.acquire(timeout=timeout):
            return True
        self._decref(key)
        return False

    def release(self, key: str) -> None:
        with self._guard:
            lock, refs = self._entries[key]
            lock.release()
            if refs <= 1:
                del self._entries[key]
            else:
                self._entries[key] = (lock, refs - 1)

    def _decref(self, key: str) -> None:
        with self._guard:
            lock, refs = self._entries[key]
            if refs <= 1:
                del self._entries[key]
            else:
                self._entries[key] = (lock, refs - 1)


class RenderWorker:
    """Build image derivatives with optional retention coordination and metadata state."""

    def __init__(
        self,
        layout: CacheLayout,
        *,
        executor: ImageExecutor | None = None,
        limits: WorkerLimits | None = None,
        state: WorkerState | None = None,
        busy_timeout: float = 2.0,
        management: ManagementClient | None = None,
    ) -> None:
        self.layout = layout
        self.raw_dir = self._default_raw_dir(layout)
        self.executor = executor or VipsExecutor()
        self.limits = limits or WorkerLimits()
        self.state = state
        self.busy_timeout = busy_timeout
        self._locks = _KeyedLocks()
        self.management = management

    def ensure(self, spec: ImageSpec, *, retain: RetainPolicy | str = RetainPolicy.TTL) -> Path:
        """Guarantee requested intermediates and apply the explicit leaf retention policy."""
        try:
            policy = RetainPolicy(retain)
        except ValueError as error:
            raise InvalidSpecError("retain must be ttl or pin") from error
        return self._render(spec, retain=policy, ensure=True)

    def materialize(self, spec: ImageSpec) -> Path:
        """Return or render a leaf, caching requested intermediates on a best-effort basis."""
        return self._render(spec, retain=RetainPolicy.TTL, ensure=False)

    def _render(self, spec: ImageSpec, *, retain: RetainPolicy, ensure: bool) -> Path:
        """Serialize a leaf request and publish under shared owner coordination."""
        self._validate_spec(spec)
        leaf_path = self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)
        leaf_key = spec.leaf.key
        deadline = time.monotonic() + self.busy_timeout
        if not self._locks.acquire(leaf_key, timeout=max(0, deadline - time.monotonic())):
            if leaf_path.exists() and not ensure:
                return leaf_path
            raise WorkerBusyError(retry_after=self.busy_timeout)
        try:
            with self._session(spec, retain=retain, deadline=deadline) as session:
                built = self._ensure_nodes(spec, session) if ensure else []
                built.extend(self._ensure_leaf(spec, leaf_path, session, retain))
                if self.state is not None:
                    self.state.record_materialize(spec, list(dict.fromkeys(built)))
                return leaf_path
        except BusyError as error:
            raise WorkerBusyError(self.busy_timeout) from error
        finally:
            self._locks.release(leaf_key)

    def _ensure_nodes(self, spec: ImageSpec, session: ManagementSession | None) -> list[str]:
        """Materialize every requested intermediate, including those bypassed by a deeper hit."""
        built_keys: list[str] = []
        for index, node in enumerate(spec.nodes):
            if not self._should_materialize_node(node):
                continue
            target = self._node_path(node)
            existing = next((p for p in self._candidate_node_paths(node) if p.exists()), None)
            if existing is not None:
                if node.materialize == MaterializePolicy.PIN and not target.exists():
                    self._publish(
                        target, partial(self._promote, existing), session, RetainPolicy.PIN
                    )
                continue
            prefix = replace(spec, operations=spec.operations[: index + 1])
            _, built = self._build_image(prefix, materialize_nodes=True, session=session)
            built_keys.extend(built)
        return built_keys

    @staticmethod
    def _promote(existing: Path, temporary: Path) -> None:
        temporary.unlink()
        os.link(existing, temporary)

    def _ensure_leaf(
        self, spec: ImageSpec, path: Path, session: ManagementSession | None, retain: RetainPolicy
    ) -> list[str]:
        """Reuse an existing leaf or encode and publish a missing one."""
        if path.exists():
            if session is not None:
                session.record(self._rel(path), retain)
            return []
        image, built = self._build_image(spec, materialize_nodes=True, session=session)
        self.limits.check_output_image(image)
        if self.state is not None:
            try:
                self.state.put_meta(spec.parent_key, self.executor.metadata(image))
            except Exception as error:
                _LOGGER.warning(
                    "opportunistic materialize metadata memo failed: %s", error, exc_info=True
                )
        self._publish(path, partial(self.executor.write_leaf, image, spec.leaf), session, retain)
        return built

    def _write_keys(self, spec: ImageSpec, *, pessimistic: bool = False) -> set[str]:
        keys: set[str] = set()
        for node in spec.nodes:
            if not self._should_materialize_node(node):
                continue
            candidates = (
                (self._node_path(node),)
                if node.materialize == MaterializePolicy.PIN
                else self._candidate_node_paths(node)
            )
            if pessimistic or not any(path.exists() for path in candidates):
                keys.add(node.key)
        return keys

    @contextmanager
    def _session(
        self,
        spec: ImageSpec,
        *,
        retain: RetainPolicy = RetainPolicy.TTL,
        deadline: float | None = None,
        writing: bool = True,
    ) -> Generator[ManagementSession | None]:
        """Lock the chain within one deadline, retrying once if write requirements change."""
        if self.management is None:
            if retain == RetainPolicy.PIN or self.layout.management_marker.exists():
                raise VipsCacheError(
                    ErrorType.MANAGEMENT_UNAVAILABLE, "this cache requires its retention owner"
                )
            yield None
            return
        deadline = time.monotonic() + self.busy_timeout if deadline is None else deadline
        read = [node.key for node in spec.nodes]
        for attempt in range(2):
            if attempt and time.monotonic() >= deadline:
                raise BusyError("artifact lock deadline exceeded")
            write = self._write_keys(spec, pessimistic=bool(attempt)) if writing else set()
            if writing and spec.encode is not None:
                write.add(spec.leaf.key)
            with self.management.session(
                list(write), max(0, deadline - time.monotonic()), read=read
            ) as session:
                if writing and not self._write_keys(spec).issubset(write):
                    # Never upgrade. Release the whole session, then retry once
                    # with every explicitly materialized node held exclusively.
                    continue
                yield session
                return
        raise BusyError("artifact requirements changed while acquiring locks")

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.layout.root).as_posix()

    def _publish(
        self,
        path: Path,
        writer: Callable[[Path], None],
        session: ManagementSession | None,
        retain: RetainPolicy = RetainPolicy.TTL,
    ) -> None:
        if session is None:
            atomic_write(path, writer)
        else:
            session.publish(self.layout, path, writer, retain)

    def unpin(self, spec: ImageSpec) -> None:
        self._manage_leaves([spec], "unpin")

    def delete(self, specs: list[ImageSpec]) -> None:
        self._manage_leaves(specs, "delete")

    def _manage_leaves(self, specs: list[ImageSpec], method: str) -> None:
        if self.management is None:
            raise VipsCacheError(ErrorType.MANAGEMENT_UNAVAILABLE, "retention owner is required")
        for spec in specs:
            self._validate_spec(spec)
        with self.management.session(
            [spec.leaf.key for spec in specs], self.busy_timeout
        ) as session:
            for spec in specs:
                session.request(
                    {
                        "method": method,
                        "path": self._rel(
                            self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)
                        ),
                    }
                )

    def measure(self, spec: ImageSpec) -> dict[str, Any]:
        """Build the pipeline lazily (no encode) and read libvips header metadata.

        Reuses the exact materialize render path so reported geometry is identical
        to the geometry of the leaf this spec would produce.
        """
        try:
            validate_file_id(spec.source.file_id)
            for node in spec.nodes:
                self.limits.check_node(node)
        except VipsCacheError:
            raise
        except (ValueError, TypeError) as error:
            raise InvalidSpecError(str(error)) from error
        if self.state is not None:
            meta = self.state.get_meta(spec.parent_key)
            if meta is not None:
                self.state.record_identify(spec)
                return meta
        with self._session(spec, writing=False) as session:
            image, _built_node_keys = self._build_image(
                spec, materialize_nodes=False, session=session
            )
            meta = self.executor.metadata(image)
        if self.state is not None:
            self.state.put_meta(spec.parent_key, meta)
            self.state.record_identify(spec)
        return meta

    def _build_image(
        self, spec: ImageSpec, *, materialize_nodes: bool, session: ManagementSession | None = None
    ) -> tuple[Any, list[str]]:
        """Reuse the deepest available parent and evaluate the remaining image operations."""
        built_node_keys: list[str] = []
        image, start_at = self._load_deepest_parent(spec)
        if image is None and spec.nodes and spec.nodes[0].operation.name in {"render", "thumbnail"}:
            first_node = spec.nodes[0]
            source_path = self._source_path(spec.source)
            if first_node.operation.name == "thumbnail":
                header = self.executor.load_source(source_path)
                self.limits.check_input_image(header)
                image = self.executor.thumbnail_source(source_path, first_node, header)
            else:
                if not spec.source.is_pdf():
                    self.limits.check_input_image(self.executor.load_source(source_path))
                image = self.executor.render_source(spec.source, source_path, first_node)
                self.limits.check_input_image(image)
            self.limits.check_output_image(image)
            if materialize_nodes and self._should_materialize_node(first_node):
                if self._write_node_if_missing(first_node, image, session):
                    built_node_keys.append(first_node.key)
            start_at = 1
        elif image is None:
            image = self.executor.load_source(self._source_path(spec.source))
            self.limits.check_input_image(image)

        for node in spec.nodes[start_at:]:
            image = self.executor.apply_node(image, node)
            self.limits.check_output_image(image)
            if materialize_nodes and self._should_materialize_node(node):
                if self._write_node_if_missing(node, image, session):
                    built_node_keys.append(node.key)
        return image, built_node_keys

    def _write_node_if_missing(
        self, node: NodeSpec, image: Any, session: ManagementSession | None = None
    ) -> bool:
        path = self._node_path(node)
        if not path.exists():
            self._publish(
                path,
                lambda tmp: self.executor.write_node(image, tmp),
                session,
                RetainPolicy.PIN if node.materialize == MaterializePolicy.PIN else RetainPolicy.TTL,
            )
            return True
        return False

    def _validate_spec(self, spec: ImageSpec) -> None:
        try:
            if spec.encode is None:
                raise InvalidSpecError("ImageSpec must include an encode to be materialized")
            validate_file_id(spec.source.file_id)
            self.limits.check_leaf(spec.leaf)
            for node in spec.nodes:
                self.limits.check_node(node)
        except VipsCacheError:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise InvalidSpecError(str(error)) from error

    def _load_deepest_parent(self, spec: ImageSpec):
        for index in range(len(spec.nodes) - 1, -1, -1):
            node = spec.nodes[index]
            for path in self._candidate_node_paths(node):
                try:
                    return self.executor.load_node(path), index + 1
                except FileNotFoundError:
                    continue

        return None, 0

    def _should_materialize_node(self, node: NodeSpec) -> bool:
        return node.materialize in {MaterializePolicy.FORCE, MaterializePolicy.PIN}

    def _node_path(self, node: NodeSpec) -> Path:
        return self.layout.node_path(node.key, pinned=node.materialize == MaterializePolicy.PIN)

    def _candidate_node_paths(self, node: NodeSpec) -> tuple[Path, ...]:
        return (
            self.layout.node_path(node.key, pinned=True),
            self.layout.node_path(node.key, pinned=False),
        )

    def _source_path(self, source: SourceSpec) -> Path:
        path = self.raw_dir / validate_file_id(source.file_id)
        try:
            # Distinguish a missing source from missing cached intermediates.
            with path.open("rb"):
                pass
        except FileNotFoundError as error:
            raise VipsCacheError(ErrorType.ORIGINAL_MISSING, str(path)) from error
        return path

    def _default_raw_dir(self, layout: CacheLayout) -> Path:
        if layout.root.name == "cache":
            return layout.root.parent / "raw"
        return layout.root / "raw"
