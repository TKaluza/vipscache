from __future__ import annotations

from pathlib import Path
from urllib.request import Request, urlopen

import pyvips

from vipscache import CacheLayout, ImageSpec, VipsCacheClient, MaterializePolicy, Operation, RenderWorker

PDF_URL = "https://ontheline.trincoll.edu/images/bookdown/sample-local-pdf.pdf"
IMAGE_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/Example_image_not_to_be_used_in_article_namespace.jpg"


def download(url: str, path: Path) -> Path:
    request = Request(url, headers={"User-Agent": "vipscache-test-suite/0.1"})
    with urlopen(request, timeout=30) as response:
        path.write_bytes(response.read())
    return path


def test_real_pdf_page_renders_to_png(tmp_path):
    root = tmp_path / "shared"
    pdf = download(PDF_URL, tmp_path / "sample-local-pdf.pdf")
    source = VipsCacheClient(root).register(pdf, mime="application/pdf").source
    spec = ImageSpec.canonical(
        source,
        [Operation("render", {"page": 1, "dpi": 75, "colorspace": "srgb"}, MaterializePolicy.FORCE)],
        Operation("encode", {"format": "png"}),
    )

    layout = CacheLayout(root / "cache")
    output = RenderWorker(layout).materialize(spec)

    assert output.exists()
    assert layout.node_path(spec.nodes[0].key).exists()
    image = pyvips.Image.new_from_file(str(output))
    assert image.width > 0
    assert image.height > 0


def test_real_pdf_identify_matches_rendered_leaf(tmp_path):
    """vipscache-reported page geometry must equal the actual rendered leaf size.

    This is the structural guarantee against the pypdf-mediabox vs libvips-render
    mismatch (e.g. /Rotate axis swap): a consumer that crops against identify()
    can never exceed the real rendered bounds.
    """
    root = tmp_path / "shared"
    pdf = download(PDF_URL, tmp_path / "sample-local-pdf.pdf")
    worker = RenderWorker(CacheLayout(root / "cache"))
    client = VipsCacheClient(root, worker)

    page = client.register(pdf, mime="application/pdf").page(1, dpi=75)
    reported = page.size
    assert page.n_pages >= 1

    output = page.png().path()
    rendered = pyvips.Image.new_from_file(str(output))
    assert reported == (rendered.width, rendered.height)


def test_real_commons_image_derivative_to_webp(tmp_path):
    root = tmp_path / "shared"
    jpg = download(IMAGE_URL, tmp_path / "commons-example.jpg")
    source = VipsCacheClient(root).register(jpg, mime="image/jpeg").source
    spec = ImageSpec.canonical(
        source,
        [
            Operation("render", {"longest_edge": 256, "colorspace": "srgb"}, MaterializePolicy.FORCE),
            Operation("colorspace", {"colorspace": "srgb"}),
            Operation("scale", {"longest_edge": 256}, MaterializePolicy.FORCE),
            Operation("crop", {"x": 0, "y": 0, "w": 128, "h": 128}, MaterializePolicy.NEVER),
            Operation("fast_rotate", {"degrees": 90}, MaterializePolicy.NEVER),
        ],
        Operation("encode", {"format": "webp", "quality": 80}),
    )

    output = RenderWorker(CacheLayout(root / "cache")).materialize(spec)

    assert output.exists()
    image = pyvips.Image.new_from_file(str(output))
    assert (image.width, image.height) == (256, 256)
