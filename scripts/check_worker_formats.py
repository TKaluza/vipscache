"""Decode real encoded files through the installed worker (no mocked loaders)."""

import argparse
import json
import tempfile
from pathlib import Path

import pyvips

from vipscache import CacheLayout, RenderWorker, VipsCacheClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--iphone", type=Path, help="Optional externally supplied iPhone HEIC fixture"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        xy = pyvips.Image.xyz(64, 48)
        image = (
            (xy[0] * 3)
            .bandjoin([xy[1] * 4, xy[0] + xy[1]])
            .cast("uchar")
            .copy(interpretation="srgb")
        )
        root = work / "shared"
        client = VipsCacheClient(root, RenderWorker(CacheLayout(root / "cache")))
        checks = []
        for name, extension, options in [
            ("JPEG", "jpg", {}),
            ("PNG", "png", {}),
            ("TIFF", "tif", {}),
            ("WebP", "webp", {}),
            ("JPEG XL", "jxl", {}),
            ("AVIF", "avif", {"compression": "av1", "effort": 0}),
            ("HEIC-8", "heic", {"compression": "hevc", "bitdepth": 8, "effort": 0}),
            ("HEIC-10", "heic", {"compression": "hevc", "bitdepth": 10, "effort": 0}),
        ]:
            path = work / f"{name}.{extension}"
            image.write_to_file(str(path), **options)
            decoded = pyvips.Image.new_from_file(str(path))
            assert (decoded.width, decoded.height) == (64, 48)
            if name.startswith("HEIC"):
                assert decoded.get("heif-bitdepth") == options["bitdepth"]
            output = client.register(path).thumbnail(longest_edge=32).webp().path()
            preview = pyvips.Image.new_from_file(str(output))
            assert (preview.width, preview.height) == (32, 24)
            checks.append(
                {
                    "format": name,
                    "bytes": path.stat().st_size,
                    "loader": decoded.get("vips-loader"),
                    "preview": [preview.width, preview.height],
                }
            )
        if args.iphone:
            decoded = pyvips.Image.new_from_file(str(args.iphone))
            result = client.register(args.iphone).thumbnail(longest_edge=320).png().path()
            preview = pyvips.Image.new_from_file(str(result))
            assert max(preview.width, preview.height) == 320
            checks.append(
                {
                    "format": "iPhone HEIC",
                    "source": [decoded.width, decoded.height],
                    "bitdepth": decoded.get("heif-bitdepth"),
                    "preview": [preview.width, preview.height],
                }
            )
        print(
            json.dumps(
                {"libvips": ".".join(str(pyvips.version(i)) for i in range(3)), "checks": checks},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
