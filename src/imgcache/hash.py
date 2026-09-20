from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import xxhash


def canonical_json(value: Any) -> bytes:
    """Return stable JSON bytes for hashing specs."""
    return json.dumps(
        _normalize_numbers(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalize_numbers(value: Any) -> Any:
    # JSON/JavaScript has one number type: 1.0 and -0.0 arrive as 1 and 0.
    # Keep integer-valued floats in the same hash domain in both clients.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_numbers(item) for item in value]
    return value


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
