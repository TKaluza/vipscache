"""Reproducible 24 MP thumbnail/scale timing and peak RSS, isolated per run.

Run with the worker Python: python scripts/benchmark_thumbnail.py [--runs 3].
Each render uses fresh paths, no vips operation cache, one vips thread. File
cache is deliberately warm in both cases. Source generation is not timed.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run_one(source: Path, mode: str) -> dict:
    import pyvips

    from vipscache import CacheLayout, RenderWorker, VipsCacheClient

    pyvips.cache_set_max(0)
    with tempfile.TemporaryDirectory() as root:
        client = VipsCacheClient(root, RenderWorker(CacheLayout(Path(root) / "cache")))
        image = client.link(source)
        begin = time.perf_counter()
        output = getattr(image, mode)(longest_edge=320).png().path()
        elapsed = time.perf_counter() - begin
        result = pyvips.Image.new_from_file(str(output))
        return {
            "method": mode,
            "seconds": elapsed,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "size": [result.width, result.height],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--child", choices=["scale", "thumbnail"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--generate", type=Path)
    args = parser.parse_args()
    if args.generate:
        import pyvips

        xy = pyvips.Image.xyz(6000, 4000)
        pixels = (xy[0] % 256).bandjoin([xy[1] % 256, (xy[0] + xy[1]) % 256]).cast("uchar")
        pixels.jpegsave(str(args.generate), Q=92)
        return
    if args.child:
        print(json.dumps(run_one(args.source, args.child)))
        return
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "24mp.jpg"
        # Generate in a different process: inherited RSS would otherwise set
        # the same artificial high-water floor for every benchmark child.
        subprocess.run([sys.executable, __file__, "--generate", str(source)], check=True)
        measurements = []
        for _ in range(args.runs):
            for mode in ("scale", "thumbnail"):
                result = subprocess.check_output(
                    [sys.executable, __file__, "--child", mode, "--source", str(source)],
                    env={**os.environ, "VIPS_CONCURRENCY": "1"},
                )
                measurements.append(json.loads(result))
        import pyvips

        from vipscache import CacheLayout, RenderWorker, VipsCacheClient

        root = Path(directory) / "cache-root"
        client = VipsCacheClient(root, RenderWorker(CacheLayout(root / "cache")))
        base = client.link(source).thumbnail(longest_edge=2560).materialize("force")
        base.webp().path()
        node_bytes = client.layout.node_path(base.spec.nodes[0].key).stat().st_size
        summary = {
            mode: {
                field: statistics.median(
                    row[field] for row in measurements if row["method"] == mode
                )
                for field in ("seconds", "peak_rss_mib")
            }
            for mode in ("scale", "thumbnail")
        }
        print(
            json.dumps(
                {
                    "libvips": ".".join(str(pyvips.version(i)) for i in range(3)),
                    "source": [6000, 4000],
                    "node_2560_bytes": node_bytes,
                    "medians": summary,
                    "runs": measurements,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
