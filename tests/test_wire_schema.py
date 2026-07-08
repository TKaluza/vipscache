from __future__ import annotations

import json
from pathlib import Path

from imgcache.hash import xxh3_128_hexdigest
from imgcache.layout import CacheLayout
from imgcache.spec import ImageSpec

GOLDEN_DIR = Path(__file__).parent / "golden"


def _load_spec_fixtures() -> list[dict[str, object]]:
    fixtures: list[dict[str, object]] = []
    with (GOLDEN_DIR / "specs.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                fixtures.append(json.loads(line))
    return fixtures


def _hash_vector_bytes(name: str) -> bytes:
    if name == "empty":
        return b""
    if name == "short_ascii":
        return b"imgcache"
    if name == "binary_0_255":
        return bytes(range(256))
    if name == "multi_mb_pattern":
        return b"imgcache-golden-vector\0\xff" * 131072
    raise AssertionError(f"unknown hash vector {name!r}")


def test_golden_spec_payloads_match_current_wire_schema_and_relpaths():
    fixtures = _load_spec_fixtures()

    assert len(fixtures) >= 20
    for fixture in fixtures:
        payload = fixture["spec"]
        restored = ImageSpec.from_payload(payload)  # type: ignore[arg-type]

        assert restored.to_payload() == payload
        assert restored.encode is not None
        expected_relpath = CacheLayout("cache").leaf_relpath(
            restored.leaf.key,
            restored.leaf.extension,
        ).as_posix()
        assert fixture["relpath"] == expected_relpath


def test_golden_specs_cover_phase_one_operations_and_encodes():
    fixtures = _load_spec_fixtures()
    operation_names = {
        operation["name"]
        for fixture in fixtures
        for operation in fixture["spec"]["operations"]  # type: ignore[index]
    }
    encode_formats = {fixture["spec"]["encode"]["format"] for fixture in fixtures}  # type: ignore[index]

    assert operation_names >= {
        "render",
        "normalize",
        "scale",
        "resize",
        "crop",
        "crop_fraction",
        "rotate",
        "fast_rotate",
        "flip",
        "flop",
    }
    assert encode_formats >= {"webp", "png", "jpg", "avif", "tif"}


def test_hash_vectors_match_python_xxh3_128():
    with (GOLDEN_DIR / "hashes.json").open(encoding="utf-8") as handle:
        vectors = json.load(handle)

    assert set(vectors) == {"empty", "short_ascii", "binary_0_255", "multi_mb_pattern"}
    for name, vector in vectors.items():
        data = _hash_vector_bytes(name)
        assert vector["length"] == len(data)
        assert vector["xxh3_128_hex"] == xxh3_128_hexdigest(data)
        assert len(vector["xxh3_128_hex"]) == 32
