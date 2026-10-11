"""Run 'write' in the previous image and 'read' in the new image on one volume."""

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pyvips

from vipscache import (
    CacheLayout,
    ImageSpec,
    MaterializePolicy,
    Operation,
    RenderWorker,
    VipsCacheClient,
)


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["write", "read"])
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    layout = CacheLayout(root / "cache")
    worker = RenderWorker(layout)
    client = VipsCacheClient(root, worker)
    version = ".".join(str(pyvips.version(i)) for i in range(3))
    manifest = root / "compatibility.json"
    if args.phase == "write":
        xy = pyvips.Image.xyz(900, 600)
        pixels = (xy[0] % 256).bandjoin([xy[1] % 256, (xy[0] + xy[1]) % 256]).cast("uchar")
        source_path = root / "source.jpg"
        pixels.jpegsave(str(source_path), Q=92)
        source = client.register(source_path).source
        results = []
        for name, ops, fmt in [
            ("scale", [Operation("scale", {"longest_edge": 320}, MaterializePolicy.FORCE)], "png"),
            (
                "crop-rotate",
                [
                    Operation("fast_rotate", {"degrees": 90}, MaterializePolicy.PIN),
                    Operation("crop", {"x": 10, "y": 20, "w": 128, "h": 96}),
                ],
                "webp",
            ),
            (
                "normalize",
                [
                    Operation("normalize", {"colorspace": "srgb"}, MaterializePolicy.FORCE),
                    Operation("scale", {"width": 321}),
                ],
                "jpg",
            ),
        ]:
            spec = ImageSpec.build(source, ops, Operation("encode", {"format": fmt}))
            path = worker.materialize(spec)
            results.append(
                {
                    "name": name,
                    "spec": spec.to_payload(),
                    "key": spec.leaf.key,
                    "path": path.relative_to(root).as_posix(),
                    "sha256": checksum(path),
                }
            )
        manifest.write_text(json.dumps({"libvips": version, "cases": results}, indent=2))
        print(manifest.read_text())
        return

    old = json.loads(manifest.read_text())

    class OfflineWorker:
        def materialize(self, spec):
            raise AssertionError("old cache hit attempted to render")

    offline = VipsCacheClient(root, OfflineWorker())
    fresh_root = root / "fresh"
    fresh_worker = RenderWorker(CacheLayout(fresh_root / "cache"))
    fresh = VipsCacheClient(fresh_root, fresh_worker)
    fresh.register(root / "source.jpg")
    result = []
    for case in old["cases"]:
        spec = ImageSpec.from_payload(case["spec"])
        plain = replace(
            spec,
            operations=tuple(
                replace(op, materialize=MaterializePolicy.NEVER) for op in spec.operations
            ),
        )
        assert plain.leaf.key == case["key"]
        path = offline.get(plain)
        assert path.relative_to(root).as_posix() == case["path"]
        assert checksum(path) == case["sha256"]
        new_path = fresh_worker.materialize(spec)
        before, after = [pyvips.Image.new_from_file(str(p)) for p in (path, new_path)]
        assert (before.width, before.height, before.bands) == (
            after.width,
            after.height,
            after.bands,
        )
        delta = (before.cast("float") - after.cast("float")).abs()
        result.append(
            {
                "name": case["name"],
                "old_hit_unchanged": True,
                "fresh_bytes_equal": checksum(path) == checksum(new_path),
                "fresh_pixels_max_delta": delta.max(),
                "fresh_pixels_mean_delta": delta.avg(),
            }
        )
    # Reuse old native nodes with the original unavailable.
    (root / "raw" / spec.source.file_id).unlink()
    for case in old["cases"]:
        spec = ImageSpec.from_payload(case["spec"])
        spec = replace(spec, encode=replace(spec.leaf, format="png", params={"compression": 1}))
        assert worker.materialize(spec).exists()
    print(
        json.dumps(
            {
                "old_libvips": old["libvips"],
                "new_libvips": version,
                "old_nodes_reused_without_original": True,
                "cases": result,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
