from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Any

from imgcache.executor import ImageExecutor, VipsExecutor
from imgcache.io import atomic_write
from imgcache.layout import CacheLayout
from imgcache.limits import WorkerLimits
from imgcache.spec import ImageSpec, MaterializePolicy, NodeSpec, SourceSpec


class RenderWorker:
    def __init__(
        self,
        layout: CacheLayout,
        *,
        executor: ImageExecutor | None = None,
        limits: WorkerLimits | None = None,
    ) -> None:
        self.layout = layout
        self.raw_dir = self._default_raw_dir(layout)
        self.executor = executor or VipsExecutor()
        self.limits = limits or WorkerLimits()
        self._locks: defaultdict[str, Lock] = defaultdict(Lock)

    def materialize(self, spec: ImageSpec) -> Path:
        if spec.encode is None:
            raise ValueError("ImageSpec must include an encode to be materialized")
        self._validate_spec(spec)
        leaf_path = self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)
        lock = self._locks[spec.leaf.key]
        with lock:
            if leaf_path.exists():
                return leaf_path

            image = self._build_image(spec, materialize_nodes=True)
            atomic_write(leaf_path, lambda tmp: self.executor.write_leaf(image, spec.leaf, tmp))
            return leaf_path

    def measure(self, spec: ImageSpec) -> dict[str, Any]:
        """Build the pipeline lazily (no encode) and read libvips header metadata.

        Reuses the exact materialize render path so reported geometry is identical
        to the geometry of the leaf this spec would produce.
        """
        for node in spec.nodes:
            self.limits.check_node(node)
        image = self._build_image(spec, materialize_nodes=False)
        return self.executor.metadata(image)

    def _build_image(self, spec: ImageSpec, *, materialize_nodes: bool) -> Any:
        image, start_at = self._load_deepest_parent(spec)
        if image is None and spec.nodes and spec.nodes[0].operation.name == "render":
            first_node = spec.nodes[0]
            image = self.executor.render_source(spec.source, self._source_path(spec.source), first_node)
            self.limits.check_output_image(image)
            if materialize_nodes and self._should_materialize_node(first_node):
                self._write_node_if_missing(first_node, image)
            start_at = 1
        elif image is None:
            image = self.executor.load_source(self._source_path(spec.source))
            self.limits.check_input_image(image)

        for node in spec.nodes[start_at:]:
            image = self.executor.apply_node(image, node)
            self.limits.check_output_image(image)
            if materialize_nodes and self._should_materialize_node(node):
                self._write_node_if_missing(node, image)
        return image

    def _write_node_if_missing(self, node: NodeSpec, image: Any) -> None:
        path = self._node_path(node)
        if not path.exists():
            atomic_write(path, lambda tmp: self.executor.write_node(image, tmp))

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
        if node.materialize in {MaterializePolicy.FORCE, MaterializePolicy.PIN}:
            return True
        return False

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
