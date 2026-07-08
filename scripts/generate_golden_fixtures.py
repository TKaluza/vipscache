from __future__ import annotations

import json
from pathlib import PurePosixPath

from imgcache.hash import xxh3_128_hexdigest
from imgcache.layout import CacheLayout
from imgcache.spec import ImageSpec, Operation, SourceSpec


def _leaf_relpath(spec: ImageSpec) -> str:
    return CacheLayout(PurePosixPath("cache")).leaf_relpath(spec.leaf.key, spec.leaf.extension).as_posix()


def _case(source: SourceSpec, operations: list[Operation], encode: Operation) -> dict[str, object]:
    spec = ImageSpec.build(source, operations, encode)
    return {"spec": spec.to_payload(), "relpath": _leaf_relpath(spec)}


def spec_fixtures() -> list[dict[str, object]]:
    image = SourceSpec(
        "0123456789abcdeffedcba9876543210",
        mime="image/jpeg",
        metadata={"filename": "sample.jpg", "width": 4032, "height": 3024},
    )
    png_image = SourceSpec(
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        mime="image/png",
        metadata={"filename": "transparent.png", "alpha": True},
    )
    pdf = SourceSpec(
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        mime="application/pdf",
        metadata={"filename": "brochure.pdf", "pages": 12},
    )

    return [
        _case(image, [], Operation("encode", {"format": "webp", "quality": 82})),
        _case(image, [], Operation("encode", {"format": "png"})),
        _case(image, [], Operation("encode", {"format": "jpg", "quality": 85})),
        _case(image, [], Operation("encode", {"format": "avif", "quality": 60})),
        _case(image, [], Operation("encode", {"format": "tif", "compression": "lzw"})),
        _case(image, [Operation("normalize", {"colorspace": "srgb"})], Operation("encode", {"format": "webp", "quality": 90})),
        _case(image, [Operation("scale", {"longest_edge": 1600})], Operation("encode", {"format": "jpg", "quality": 76, "strip": True})),
        _case(image, [Operation("scale", {"scale_factor": 0.5})], Operation("encode", {"format": "png"})),
        _case(image, [Operation("resize", {"width": 320, "height": 240})], Operation("encode", {"format": "webp", "quality": 70})),
        _case(image, [Operation("crop", {"x": 10, "y": 20, "w": 300, "h": 200})], Operation("encode", {"format": "png"})),
        _case(image, [Operation("crop_fraction", {"left": 0.125, "top": 0.0, "right": 0.875, "bottom": 0.5})], Operation("encode", {"format": "avif", "quality": 55})),
        _case(image, [Operation("rotate", {"degrees": 12.5})], Operation("encode", {"format": "jpg", "quality": 88})),
        _case(image, [Operation("fast_rotate", {"degrees": 90})], Operation("encode", {"format": "webp", "quality": 82})),
        _case(image, [Operation("flip")], Operation("encode", {"format": "png"})),
        _case(image, [Operation("flop")], Operation("encode", {"format": "png"})),
        _case(image, [Operation("crop", {"x": 0, "y": 0, "w": 640, "h": 480}), Operation("resize", {"width": 320})], Operation("encode", {"format": "webp", "quality": 82})),
        _case(image, [Operation("resize", {"width": 320}), Operation("crop", {"x": 0, "y": 0, "w": 640, "h": 480})], Operation("encode", {"format": "webp", "quality": 82})),
        _case(png_image, [Operation("normalize", {"colorspace": "srgb"}), Operation("crop_fraction", {"left": 0.0, "top": 0.25, "right": 1.0, "bottom": 1.0})], Operation("encode", {"format": "png", "palette": False})),
        _case(pdf, [Operation("render", {"page": 1})], Operation("encode", {"format": "webp", "quality": 82})),
        _case(pdf, [Operation("render", {"page": 3, "dpi": 144}), Operation("normalize", {"colorspace": "srgb"}), Operation("scale", {"width": 1024})], Operation("encode", {"format": "jpg", "quality": 80})),
        _case(pdf, [Operation("render", {"page": 2, "dpi": 72.5}), Operation("fast_rotate", {"degrees": 270}), Operation("crop", {"x": 5, "y": 7, "w": 800, "h": 600})], Operation("encode", {"format": "tif"})),
    ]


def hash_vectors() -> dict[str, dict[str, object]]:
    vectors = {
        "empty": b"",
        "short_ascii": b"imgcache",
        "binary_0_255": bytes(range(256)),
        "multi_mb_pattern": ((b"imgcache-golden-vector" + b"\0\xff") * 131072),
    }
    return {
        name: {
            "description": name.replace("_", " "),
            "length": len(data),
            "xxh3_128_hex": xxh3_128_hexdigest(data),
        }
        for name, data in vectors.items()
    }


def main() -> None:
    golden = PurePosixPath("tests/golden")
    with open(golden / "specs.jsonl", "w", encoding="utf-8") as handle:
        for fixture in spec_fixtures():
            handle.write(json.dumps(fixture, sort_keys=True, separators=(",", ":")) + "\n")
    with open(golden / "hashes.json", "w", encoding="utf-8") as handle:
        json.dump(hash_vectors(), handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
