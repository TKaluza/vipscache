from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import xxhash


def canonical_json(value: Any) -> bytes:
    """Return stable JSON bytes for hashing specs."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def xxh3_64_hexdigest(data: bytes) -> str:
    return xxhash.xxh3_64_hexdigest(data)


def hash_canonical(value: Any) -> str:
    return xxh3_64_hexdigest(canonical_json(value))


def file_id(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = xxhash.xxh3_64()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
