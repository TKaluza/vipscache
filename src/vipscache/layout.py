from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_KEY = re.compile(r"[0-9a-f]{32}")
_ARTIFACT = re.compile(
    r"(nodes|pinned|leaves)/([0-9a-f]{2})/([0-9a-f]{32})\.(v|webp|png|jpg|avif|tif|tiff)"
)
_STAGING = re.compile(r"\.([0-9a-f]{32})\.[a-zA-Z0-9_-]+\.(v|webp|png|jpg|avif|tif|tiff)")


def validate_key(value: str) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise ValueError("key/file_id must be 32 lowercase hexadecimal characters")
    return value


class CacheLayout:
    """Own the shared artifact paths, key format and retention marker conventions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @property
    def management_marker(self) -> Path:
        return self.root / ".managed.json"

    def artifact(self, rel: str) -> tuple[Path, str, str]:
        match = _ARTIFACT.fullmatch(rel)
        if not match or match[2] != match[3][:2] or ((match[1] == "leaves") == (match[4] == "v")):
            raise ValueError("invalid artifact path")
        return self.root / rel, match[3], "leaf" if match[1] == "leaves" else "node"

    def is_pinned(self, path: Path) -> bool:
        return path.relative_to(self.root).parts[0] == "pinned"

    def staging_key(self, path: Path) -> str | None:
        match = _STAGING.fullmatch(path.name)
        if match is None:
            return None
        try:
            self.artifact(
                path.with_name(f"{match[1]}.{match[2]}").relative_to(self.root).as_posix()
            )
        except ValueError:
            return None
        return match[1]

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
