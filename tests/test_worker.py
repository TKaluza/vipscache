from pathlib import Path
from threading import Event, Thread

import asyncio
import time

import pytest
import pyvips

from imgcache import CacheLayout, ImgCacheClient, MaterializePolicy, Operation, RenderWorker, WorkerBusyError
from imgcache.executor import VipsExecutor
from imgcache.limits import WorkerLimits
from imgcache.spec import ImageSpec


def make_image(path: Path) -> None:
    width = 64
    height = 48
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend((255, 0, 0) if 16 <= x < 48 and 12 <= y < 36 else (255, 255, 255))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(pixels))


def test_worker_materializes_forced_node_and_leaf(tmp_path):
    root = tmp_path / "shared"
    source_path = tmp_path / "source.ppm"
    make_image(source_path)

    source = ImgCacheClient(root).register(source_path, mime="image/png").source
    render = Operation("render", {"width": 32}, MaterializePolicy.FORCE)
    crop = Operation("crop", {"x": 4, "y": 4, "w": 16, "h": 12}, MaterializePolicy.NEVER)
    spec = ImageSpec.build(source, [render, crop], Operation("encode", {"format": "png"}))

    layout = CacheLayout(root / "cache")
    worker = RenderWorker(layout)
    path = worker.materialize(spec)

    assert path.exists()
    assert layout.node_path(spec.nodes[0].key).exists()
    assert not layout.node_path(spec.nodes[1].key).exists()

    image = pyvips.Image.new_from_file(str(path))
    assert (image.width, image.height) == (16, 12)


def test_worker_reuses_deepest_materialized_parent(tmp_path):
    root = tmp_path / "shared"
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = ImgCacheClient(root).register(source_path, mime="image/png").source
    render = Operation("render", {"width": 32}, MaterializePolicy.FORCE)
    crop = Operation("crop", {"x": 0, "y": 0, "w": 10, "h": 10}, MaterializePolicy.NEVER)
    webp = ImageSpec.build(source, [render, crop], Operation("encode", {"format": "webp", "quality": 80}))
    png = ImageSpec.build(source, [render, crop], Operation("encode", {"format": "png"}))
    layout = CacheLayout(root / "cache")
    worker = RenderWorker(layout)

    first = worker.materialize(webp)
    node_path = layout.node_path(webp.nodes[0].key)
    node_mtime = node_path.stat().st_mtime_ns
    second = worker.materialize(png)

    assert first.exists()
    assert second.exists()
    assert node_path.stat().st_mtime_ns == node_mtime


def test_img_cache_client_original_fallback_and_materialized_derivative(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    root = tmp_path / "shared"
    worker = RenderWorker(CacheLayout(root / "cache"))
    client = ImgCacheClient(root, worker)

    image = client.register(source_path, mime="image/png")
    assert image.path() == root / "raw" / image.source.file_id
    assert image.path().exists()

    preview = image.scale(width=20).png()
    path = preview.path()
    assert path.exists()
    assert preview.read_bytes() == path.read_bytes()

    with preview.open() as handle:
        assert handle.read(8).startswith(b"\x89PNG")


def test_cached_image_rejects_transform_without_encode(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    client = ImgCacheClient(tmp_path / "shared")
    image = client.register(source_path, mime="image/png").scale(width=20)

    try:
        image.path()
    except ValueError as error:
        assert "must choose an output format" in str(error)
    else:
        raise AssertionError("transformed CachedImage should require an encode")


def test_async_cached_image_api(tmp_path):
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    root = tmp_path / "shared"
    worker = RenderWorker(CacheLayout(root / "cache"))
    client = ImgCacheClient(root, worker)

    async def run():
        image = await client.aregister(source_path, mime="image/png")
        preview = image.scale(width=20).png()
        with await preview.aopen() as handle:
            assert handle.read(8).startswith(b"\x89PNG")
        assert handle.closed
        path = await preview
        data = await preview.aread_bytes()
        return path, data

    path, data = asyncio.run(run())
    assert path.exists()
    assert data.startswith(b"\x89PNG")


class SlowLeafExecutor(VipsExecutor):
    """Delays leaf writes so a concurrent duplicate request hits the busy path."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.started = Event()
        self.leaf_writes = 0

    def write_leaf(self, image, spec, path):
        self.started.set()
        self.leaf_writes += 1
        time.sleep(self.delay)
        super().write_leaf(image, spec, path)


def test_worker_duplicate_render_raises_busy_and_cleans_locks(tmp_path):
    root = tmp_path / "shared"
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = ImgCacheClient(root).register(source_path, mime="image/png").source
    spec = ImageSpec.build(
        source,
        [Operation("scale", {"width": 16})],
        Operation("encode", {"format": "png"}),
    )
    executor = SlowLeafExecutor(delay=0.5)
    worker = RenderWorker(CacheLayout(root / "cache"), executor=executor, busy_timeout=0.05)

    results = {}

    def render():
        results["path"] = worker.materialize(spec)

    thread = Thread(target=render)
    thread.start()
    assert executor.started.wait(timeout=5)

    with pytest.raises(WorkerBusyError) as excinfo:
        worker.materialize(spec)
    assert excinfo.value.retry_after == 0.05

    thread.join(timeout=5)
    assert results["path"].exists()
    assert worker._locks._entries == {}
    assert worker.materialize(spec) == results["path"]
    assert executor.leaf_writes == 1


def test_worker_busy_loser_returns_hit_when_render_finishes_in_time(tmp_path):
    root = tmp_path / "shared"
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = ImgCacheClient(root).register(source_path, mime="image/png").source
    spec = ImageSpec.build(
        source,
        [Operation("scale", {"width": 16})],
        Operation("encode", {"format": "png"}),
    )
    executor = SlowLeafExecutor(delay=0.2)
    worker = RenderWorker(CacheLayout(root / "cache"), executor=executor, busy_timeout=5.0)

    results = {}

    def render():
        results["path"] = worker.materialize(spec)

    thread = Thread(target=render)
    thread.start()
    assert executor.started.wait(timeout=5)

    duplicate = worker.materialize(spec)

    thread.join(timeout=5)
    assert duplicate == results["path"]
    assert executor.leaf_writes == 1
    assert worker._locks._entries == {}


def test_worker_enforces_limits(tmp_path):
    root = tmp_path / "shared"
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    source = ImgCacheClient(root).register(source_path, mime="image/png").source
    spec = ImageSpec.build(
        source,
        [Operation("render", {"width": 20})],
        Operation("encode", {"format": "png"}),
    )
    worker = RenderWorker(CacheLayout(root / "cache"), limits=WorkerLimits(max_output_pixels=10))

    try:
        worker.materialize(spec)
    except ValueError as error:
        assert "output image exceeds pixel limit" in str(error)
    else:
        raise AssertionError("worker should reject oversized output")
