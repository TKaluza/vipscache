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


def xxh3_128_hexdigest(data: bytes) -> str:
    return xxhash.xxh3_128_hexdigest(data)


def hash_canonical(value: Any) -> str:
    return xxh3_128_hexdigest(canonical_json(value))


def file_id(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = xxhash.xxh3_128()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()
