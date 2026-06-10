from __future__ import annotations

from pathlib import Path, PurePosixPath


class CacheLayout:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def node_relpath(self, key: str, *, pinned: bool = False) -> PurePosixPath:
        base = "pinned" if pinned else "nodes"
        return PurePosixPath(base, key[:2], f"{key}.v")

    def leaf_relpath(self, key: str, extension: str) -> PurePosixPath:
        return PurePosixPath("leaves", key[:2], f"{key}.{extension}")

    def to_native(self, relpath: PurePosixPath) -> Path:
        return self.root.joinpath(*relpath.parts)

    def node_path(self, key: str, *, pinned: bool = False) -> Path:
        return self.to_native(self.node_relpath(key, pinned=pinned))

    def leaf_path(self, key: str, extension: str) -> Path:
        return self.to_native(self.leaf_relpath(key, extension))
