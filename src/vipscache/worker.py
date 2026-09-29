from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock
from typing import Any

from vipscache.executor import ImageExecutor, VipsExecutor
from vipscache.io import atomic_write
from vipscache.layout import CacheLayout
from vipscache.limits import WorkerLimits
from vipscache.spec import ImageSpec, MaterializePolicy, NodeSpec, SourceSpec
from vipscache.state import WorkerState

_LOGGER = logging.getLogger("vipscache.worker")


class WorkerBusyError(RuntimeError):
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
    def __init__(
        self,
        layout: CacheLayout,
        *,
        executor: ImageExecutor | None = None,
        limits: WorkerLimits | None = None,
        state: WorkerState | None = None,
        busy_timeout: float = 2.0,
    ) -> None:
        self.layout = layout
        self.raw_dir = self._default_raw_dir(layout)
        self.executor = executor or VipsExecutor()
        self.limits = limits or WorkerLimits()
        self.state = state
        self.busy_timeout = busy_timeout
        self._locks = _KeyedLocks()

    def materialize(self, spec: ImageSpec) -> Path:
        if spec.encode is None:
            raise ValueError("ImageSpec must include an encode to be materialized")
        self._validate_spec(spec)
        leaf_path = self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)
        leaf_key = spec.leaf.key
        if not self._locks.acquire(leaf_key, timeout=self.busy_timeout):
            # The competing render may have finished while we waited.
            if leaf_path.exists():
                return leaf_path
            raise WorkerBusyError(retry_after=self.busy_timeout)
        try:
            if leaf_path.exists():
                return leaf_path

            image, built_node_keys = self._build_image(spec, materialize_nodes=True)
            if self.state is not None:
                try:
                    self.state.put_meta(spec.parent_key, self.executor.metadata(image))
                except Exception as error:
                    _LOGGER.warning("opportunistic materialize metadata memo failed: %s", error, exc_info=True)
            atomic_write(leaf_path, lambda tmp: self.executor.write_leaf(image, spec.leaf, tmp))
            if self.state is not None:
                self.state.record_materialize(spec, built_node_keys)
            return leaf_path
        finally:
            self._locks.release(leaf_key)

    def measure(self, spec: ImageSpec) -> dict[str, Any]:
        """Build the pipeline lazily (no encode) and read libvips header metadata.

        Reuses the exact materialize render path so reported geometry is identical
        to the geometry of the leaf this spec would produce.
        """
        for node in spec.nodes:
            self.limits.check_node(node)
        if self.state is not None:
            meta = self.state.get_meta(spec.parent_key)
            if meta is not None:
                self.state.record_identify(spec)
                return meta
        image, _built_node_keys = self._build_image(spec, materialize_nodes=False)
        meta = self.executor.metadata(image)
        if self.state is not None:
            self.state.put_meta(spec.parent_key, meta)
            self.state.record_identify(spec)
        return meta

    def _build_image(self, spec: ImageSpec, *, materialize_nodes: bool) -> tuple[Any, list[str]]:
        built_node_keys: list[str] = []
        image, start_at = self._load_deepest_parent(spec)
        if image is None and spec.nodes and spec.nodes[0].operation.name == "render":
            first_node = spec.nodes[0]
            image = self.executor.render_source(spec.source, self._source_path(spec.source), first_node)
            self.limits.check_output_image(image)
            if materialize_nodes and self._should_materialize_node(first_node):
                if self._write_node_if_missing(first_node, image):
                    built_node_keys.append(first_node.key)
            start_at = 1
        elif image is None:
            image = self.executor.load_source(self._source_path(spec.source))
            self.limits.check_input_image(image)

        for node in spec.nodes[start_at:]:
            image = self.executor.apply_node(image, node)
            self.limits.check_output_image(image)
            if materialize_nodes and self._should_materialize_node(node):
                if self._write_node_if_missing(node, image):
                    built_node_keys.append(node.key)
        return image, built_node_keys

    def _write_node_if_missing(self, node: NodeSpec, image: Any) -> bool:
        path = self._node_path(node)
        if not path.exists():
            atomic_write(path, lambda tmp: self.executor.write_node(image, tmp))
            return True
        return False

    def _validate_spec(self, spec: ImageSpec) -> None:
        self.limits.check_leaf(spec.leaf)
        for node in spec.nodes:
            self.limits.check_node(node)

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
            self.layout.node_path(node.key, pinned=node.materialize == MaterializePolicy.PIN),
            self.layout.node_path(node.key, pinned=False),
        )

    def _source_path(self, source: SourceSpec) -> Path:
        return self.raw_dir / source.file_id

    def _default_raw_dir(self, layout: CacheLayout) -> Path:
        if layout.root.name == "cache":
            return layout.root.parent / "raw"
        return layout.root / "raw"
