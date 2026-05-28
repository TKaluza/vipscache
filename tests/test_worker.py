from pathlib import Path

import pyvips

from imgcache import CacheLayout, MaterializePolicy, Operation, RenderWorker, SourceSpec, ThinClient
from imgcache.limits import WorkerLimits
from imgcache.spec import DerivativeSpec


def make_image(path: Path) -> None:
    width = 64
    height = 48
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend((255, 0, 0) if 16 <= x < 48 and 12 <= y < 36 else (255, 255, 255))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(pixels))


def test_worker_materializes_forced_node_and_leaf(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)

    source = SourceSpec.from_file(str(source_path), mime="image/png")
    render = Operation("render", {"width": 32}, MaterializePolicy.FORCE)
    crop = Operation("crop", {"x": 4, "y": 4, "w": 16, "h": 12}, MaterializePolicy.NEVER)
    spec = DerivativeSpec.build(source, [render, crop], Operation("encode", {"format": "png"}))

    layout = CacheLayout(tmp_path / "cache")
    worker = RenderWorker(layout)
    path = worker.materialize(spec)

    assert path.exists()
    assert layout.node_path(spec.nodes[0].key).exists()
    assert not layout.node_path(spec.nodes[1].key).exists()

    image = pyvips.Image.new_from_file(str(path))
    assert (image.width, image.height) == (16, 12)


def test_worker_reuses_deepest_materialized_parent(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = SourceSpec.from_file(str(source_path), mime="image/png")
    render = Operation("render", {"width": 32}, MaterializePolicy.FORCE)
    crop = Operation("crop", {"x": 0, "y": 0, "w": 10, "h": 10}, MaterializePolicy.NEVER)
    webp = DerivativeSpec.build(source, [render, crop], Operation("encode", {"format": "webp", "quality": 80}))
    png = DerivativeSpec.build(source, [render, crop], Operation("encode", {"format": "png"}))
    layout = CacheLayout(tmp_path / "cache")
    worker = RenderWorker(layout)

    first = worker.materialize(webp)
    node_path = layout.node_path(webp.nodes[0].key)
    node_mtime = node_path.stat().st_mtime_ns
    second = worker.materialize(png)

    assert first.exists()
    assert second.exists()
    assert node_path.stat().st_mtime_ns == node_mtime


def test_thin_client_reads_hit_and_delegates_miss(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = SourceSpec.from_file(str(source_path), mime="image/png")
    spec = DerivativeSpec.build(
        source,
        [Operation("render", {"width": 20}, MaterializePolicy.FORCE)],
        Operation("encode", {"format": "png"}),
    )
    layout = CacheLayout(tmp_path / "cache")
    client = ThinClient(layout, RenderWorker(layout))

    path = client.get(spec)
    assert path.exists()

    with client.open(spec) as handle:
        assert handle.read(8).startswith(b"\x89PNG")


def test_worker_enforces_limits(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = SourceSpec.from_file(str(source_path), mime="image/png")
    spec = DerivativeSpec.build(
        source,
        [Operation("render", {"width": 20})],
        Operation("encode", {"format": "png"}),
    )
    worker = RenderWorker(CacheLayout(tmp_path / "cache"), limits=WorkerLimits(max_output_pixels=10))

    try:
        worker.materialize(spec)
    except ValueError as error:
        assert "output image exceeds pixel limit" in str(error)
    else:
        raise AssertionError("worker should reject oversized output")
