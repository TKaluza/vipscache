from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from imgcache import CacheLayout, CachedImage, ImgCacheClient, RenderWorker
from imgcache.spec import SourceSpec
from imgcache.spec import ImageSpec, Operation


def make_image(path: Path, width: int = 64, height: int = 48) -> None:
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend((255, 0, 0) if 16 <= x < 48 and 12 <= y < 36 else (255, 255, 255))
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(pixels))


def make_client(tmp_path: Path) -> tuple[ImgCacheClient, Path]:
    root = tmp_path / "shared"
    worker = RenderWorker(CacheLayout(root / "cache"))
    client = ImgCacheClient(root, worker)
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    return client, source_path


def test_identify_reports_original_geometry(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    assert image.size == (64, 48)
    assert image.width == 64
    assert image.height == 48
    assert image.mode == "RGB"
    assert image.info["bands"] == 3
    assert image.info["has_alpha"] is False


def test_metadata_adapts_to_pipeline_and_resets_per_image(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    # Cache metadata on the original, then derive a new image.
    assert image.size == (64, 48)

    scaled = image.scale(width=20)
    assert scaled.size == (20, 15)  # aspect preserved: 48 * 20/64

    # Original metadata must be untouched by the derived image's fetch.
    assert image.size == (64, 48)


def test_fast_rotate_swaps_axes_via_libvips(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    assert image.fast_rotate(90).size == (48, 64)


def test_normalize_to_gray_reports_l_mode(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    gray = image.normalize(colorspace="gray")
    assert gray.mode == "L"
    assert gray.info["bands"] == 1


def test_crop_fraction_resolves_against_libvips_size(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    top_half = image.crop_fraction(bottom=0.5)
    assert top_half.size == (64, 24)

    box = image.crop_fraction(left=0.25, top=0.0, right=0.75, bottom=0.5)
    assert box.size == (32, 24)


def test_crop_fraction_rejects_out_of_range(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    with pytest.raises(ValueError, match=r"crop_fraction .* within \[0.0, 1.0\]"):
        image.crop_fraction(bottom=1.5).size


def test_pixel_crop_out_of_bounds_gives_clear_error(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap")

    with pytest.raises(ValueError, match=r"outside image bounds \(64x48\)"):
        image.crop(x=0, y=0, w=100, h=100).png().path()


def test_size_requires_page_for_pdf_source(tmp_path):
    client, _ = make_client(tmp_path)
    pdf = CachedImage(client, SourceSpec("deadbeef", mime="application/pdf"))

    with pytest.raises(ValueError, match="select a PDF page first"):
        pdf.size


def test_metadata_requires_a_worker(tmp_path):
    root = tmp_path / "shared"
    source_path = tmp_path / "source.ppm"
    make_image(source_path)
    client = ImgCacheClient(root)  # no worker

    image = client.open(source_path, mime="image/x-portable-pixmap")
    with pytest.raises(RuntimeError, match="metadata requires a worker"):
        image.size


def test_async_info(tmp_path):
    client, source_path = make_client(tmp_path)
    image = client.open(source_path, mime="image/x-portable-pixmap").scale(width=20)

    info = asyncio.run(image.ainfo())
    assert info["width"] == 20
    assert info["height"] == 15


class CountingIdentifyWorker:
    def __init__(self) -> None:
        self.calls = 0

    def identify(self, spec: ImageSpec) -> dict:
        self.calls += 1
        return {"height": 15, "mode": "RGB", "width": 20}


def test_client_metadata_memo_saves_duplicate_identify_roundtrip(tmp_path):
    worker = CountingIdentifyWorker()
    client = ImgCacheClient(tmp_path / "shared", worker)
    source = SourceSpec("f" * 32, mime="image/png")
    first = ImageSpec(source, (Operation("scale", {"width": 20}),))
    second = ImageSpec(source, (Operation("scale", {"width": 20}),))

    meta = client.identify(first)
    meta["width"] = 999

    assert client.identify(second)["width"] == 20
    assert worker.calls == 1
