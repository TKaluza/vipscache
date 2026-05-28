from __future__ import annotations

from pathlib import Path
from typing import Protocol

from imgcache.io import open_cache_hit
from imgcache.layout import CacheLayout
from imgcache.spec import DerivativeSpec


class WorkerClient(Protocol):
    def materialize(self, spec: DerivativeSpec) -> Path:
        ...


class ThinClient:
    def __init__(self, layout: CacheLayout, worker: WorkerClient | None = None) -> None:
        self.layout = layout
        self.worker = worker

    def path_for(self, spec: DerivativeSpec) -> Path:
        return self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)

    def open(self, spec: DerivativeSpec, mode: str = "rb"):
        path = self.path_for(spec)
        try:
            return open_cache_hit(path, mode)
        except FileNotFoundError:
            if self.worker is None:
                raise
            self.worker.materialize(spec)
            return open_cache_hit(path, mode)

    def get(self, spec: DerivativeSpec) -> Path:
        path = self.path_for(spec)
        try:
            with open_cache_hit(path):
                return path
        except FileNotFoundError:
            if self.worker is None:
                raise
            self.worker.materialize(spec)
            return path
