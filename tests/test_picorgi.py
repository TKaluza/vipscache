from __future__ import annotations

import asyncio
import errno
import os
from dataclasses import replace
from typing import Any

import pytest
import pyvips
from test_worker import make_image

from vipscache import (
    CacheLayout,
    RenderWorker,
    VipsCacheClient,
    VipsCacheError,
    WorkerLimits,
)
from vipscache.hash import file_id


def test_link_known_hash_never_hashes_and_unlink_preserves_derivatives(tmp_path, monkeypatch):
    source = tmp_path / "source.ppm"
    make_image(source)
    digest = file_id(source)
    root = tmp_path / "shared"
    worker = RenderWorker(CacheLayout(root / "cache"))
    client = VipsCacheClient(root, worker)
    monkeypatch.setattr("vipscache.originals.file_id", lambda _: pytest.fail("unexpected hashing"))
    linked = client.link(source, file_id=digest)
    assert source.stat().st_ino == linked.path().stat().st_ino
    derived = linked.scale(width=32).materialize("force").png()
    output = derived.path()
    node = client.layout.node_path(derived.spec.nodes[0].key)
    before = output.read_bytes(), node.stat().st_mtime_ns
    source.unlink()
    client.unlink(digest)
    client.unlink(digest)
    assert output.read_bytes() == before[0]
    assert node.stat().st_mtime_ns == before[1]
    assert derived.path() == output
    assert linked.scale(width=32).jpg().path().exists()
    with pytest.raises(VipsCacheError, match="OriginalMissing"):
        linked.scale(width=12).jpg().path()


def test_import_verification_and_dangling_links(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"known bytes")
    digest = file_id(path)
    client = VipsCacheClient(tmp_path / "shared")
    for invalid in ("../escape", "a" * 31, "A" * 32, ""):
        with pytest.raises(ValueError):
            client.register(path, file_id=invalid)
    with pytest.raises(VipsCacheError) as caught:
        client.link(path, file_id="0" * 32, verify=True)
    assert caught.value.type == "HashMismatch"
    linked = client.link(path, file_id=digest, mode="symlink", link_target="/worker/library/file")
    assert client.resolve(linked.spec).is_symlink()
    assert not client.resolve(linked.spec).exists()
    client.link(path, file_id=digest)
    assert client.resolve(linked.spec).stat().st_ino == path.stat().st_ino
    client.unlink(digest)

    def cross_device(*args):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "link", cross_device)
    with pytest.raises(VipsCacheError) as caught:
        client.link(path, file_id=digest)
    assert caught.value.type == "CrossDeviceLink"
    monkeypatch.setattr(
        "vipscache.client._memoized_file_id", lambda _: pytest.fail("unexpected hash")
    )
    assert client.register(path, file_id=digest).read_bytes() == path.read_bytes()


def test_async_link_and_unlink(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"async link")

    async def run():
        client = VipsCacheClient(tmp_path / "shared")
        image = await client.alink(path)
        assert await image.aread_bytes() == b"async link"
        await client.aunlink(image.source.file_id)
        assert not client.resolve(image.spec).exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "params,size",
    [
        ({"width": 32, "height": 32}, (32, 24)),
        ({"width": 16}, (16, 12)),
        ({"height": 12}, (16, 12)),
        ({"longest_edge": 32}, (32, 24)),
        ({"longest_edge": 128}, (128, 96)),
    ],
)
def test_thumbnail_geometry(tmp_path, params, size):
    path = tmp_path / "source.ppm"
    make_image(path)
    root = tmp_path / "shared"
    client = VipsCacheClient(root, RenderWorker(CacheLayout(root / "cache")))
    source = client.register(path)
    result = pyvips.Image.new_from_file(str(source.thumbnail(**params).png().path()))
    assert (result.width, result.height) == size
    old = pyvips.Image.new_from_file(str(source.scale(width=32, height=32).png().path()))
    assert (old.width, old.height) == (32, 32)


@pytest.mark.parametrize("orientation", range(1, 9))
def test_autorotate_all_exif_orientations(tmp_path, orientation):
    # Asymmetric gradient exposes all rotations and mirror directions.
    xy = pyvips.Image.xyz(64, 48)
    pixels = (xy[0] * 3).bandjoin([xy[1] * 4, xy[0] + xy[1]]).cast("uchar")
    pixels.set_type(pyvips.GValue.gint_type, "orientation", orientation)
    path = tmp_path / "oriented.jpg"
    pixels.jpegsave(str(path), Q=100, subsample_mode="off")
    decoded = pyvips.Image.new_from_file(str(path))
    transforms = {
        1: lambda x: x,
        2: lambda x: x.flip("horizontal"),
        3: lambda x: x.rot("d180"),
        4: lambda x: x.flip("vertical"),
        5: lambda x: x.rot("d90").flip("horizontal"),
        6: lambda x: x.rot("d90"),
        7: lambda x: x.rot("d90").flip("vertical"),
        8: lambda x: x.rot("d270"),
    }
    expected = transforms[orientation](decoded)
    root = tmp_path / "shared"
    client = VipsCacheClient(root, RenderWorker(CacheLayout(root / "cache")))
    image = client.register(path)
    actual = pyvips.Image.new_from_file(str(image.autorotate().png().path()))
    assert (actual.width, actual.height) == (expected.width, expected.height)
    assert actual.write_to_memory() == expected.write_to_memory()
    assert image.autorotate().identify().get("orientation", 1) == 1
    assert image.thumbnail(longest_edge=32).size == ((24, 32) if orientation >= 5 else (32, 24))
    assert image.thumbnail(longest_edge=32, autorotate=False).size == (32, 24)
    # Old operations retain both their geometry and their identity.
    assert image.scale(width=32).size == (32, 24)


def test_builder_and_source_limits(tmp_path):
    path = tmp_path / "source.ppm"
    make_image(path)
    root = tmp_path / "shared"
    client = VipsCacheClient(
        root, RenderWorker(CacheLayout(root / "cache"), limits=WorkerLimits(max_input_pixels=100))
    )
    source = client.register(path)
    with pytest.raises(ValueError):
        source.materialize("force")
    with pytest.raises(ValueError):
        source.scale(width=32).thumbnail(width=16)
    invalid_parameters: list[dict[str, Any]] = [
        {"width": 0},
        {"width": 1.5},
        {"width": True},
        {"longest_edge": 5, "height": 2},
        {},
    ]
    for params in invalid_parameters:
        with pytest.raises(ValueError):
            source.thumbnail(**params)
    output = source.thumbnail(longest_edge=8).png()
    with pytest.raises(VipsCacheError) as caught:
        output.path()
    assert caught.value.type == "LimitExceeded"
    assert not client.resolve(output.spec).exists()


def test_later_force_pin_and_earlier_missing_node(tmp_path):
    path = tmp_path / "source.ppm"
    make_image(path)
    root = tmp_path / "shared"
    worker = RenderWorker(CacheLayout(root / "cache"))
    client = VipsCacheClient(root, worker)
    base = client.register(path).scale(width=32)
    leaf = base.png().path()
    data, mtime = leaf.read_bytes(), leaf.stat().st_mtime_ns
    forced = base.materialize("force")
    assert forced.png().ensure() == leaf
    normal = worker.layout.node_path(forced.spec.nodes[0].key)
    assert normal.exists()
    pinned = base.materialize("pin")
    pinned.png().ensure()
    pin_path = worker.layout.node_path(pinned.spec.nodes[0].key, pinned=True)
    assert pin_path.stat().st_ino == normal.stat().st_ino
    normal.unlink()
    client.unlink(base.source.file_id)
    assert base.jpg().path().exists()  # a plain request can find a pinned node
    assert leaf.read_bytes() == data and leaf.stat().st_mtime_ns == mtime

    # A deeper cached node must not bypass a newly requested earlier node.
    source = client.register(path)
    deep = source.scale(width=32).scale(width=16).materialize("force").png()
    deep.path()
    # Remove the first node's pinned alias to require a real earlier rebuild.
    pin_path.unlink()
    modified = replace(
        deep.spec,
        operations=(replace(deep.spec.operations[0], materialize="force"), deep.spec.operations[1]),
    )
    client.ensure(modified)
    assert normal.exists()
