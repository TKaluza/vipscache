"""Kill real processes at publication boundaries; never fabricate journal rows."""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest
from test_worker import make_image

from vipscache import (
    CacheLayout,
    ImageSpec,
    ManagementService,
    RenderWorker,
    VipsCacheClient,
)
from vipscache.executor import VipsExecutor
from vipscache.originals import OriginalsStore


def _crash_render(root, directory, payload, phase, stopped, resume):
    import vipscache.catalog as catalog_module

    layout = CacheLayout(Path(root) / "cache")
    spec = ImageSpec.from_payload(payload)
    target = layout.leaf_path(spec.leaf.key, spec.leaf.extension)

    def pause():
        stopped.set()
        if not resume.wait(30):
            raise RuntimeError("crash test timed out")

    class Executor(VipsExecutor):
        def write_leaf(self, image, spec, path):
            if phase == "writing":
                path.write_bytes(b"incomplete image")
                pause()
            super().write_leaf(image, spec, path)

    with ManagementService(layout, directory) as service:
        assert service.catalog is not None
        service.catalog.wait_ready()
        original_replace = catalog_module.os.replace
        original_sync = catalog_module.sync_directory

        def replace(source, destination):
            if Path(destination) == target and phase == "before_rename":
                pause()
            return original_replace(source, destination)

        def sync(path):
            if Path(path) == target.parent and phase == "after_rename":
                pause()
            return original_sync(path)

        patch = pytest.MonkeyPatch()
        patch.setattr(catalog_module.os, "replace", replace)
        patch.setattr(catalog_module, "sync_directory", sync)
        worker = RenderWorker(layout, management=service.client, executor=Executor())
        worker.ensure(spec, retain="pin")
        if phase == "confirmed":
            pause()


@pytest.mark.parametrize("phase", ["writing", "before_rename", "after_rename", "confirmed"])
def test_real_process_kill_and_recovery(tmp_path, phase):
    root, directory = tmp_path / "shared", tmp_path / "retention"
    original = tmp_path / "source.ppm"
    make_image(original)
    client = VipsCacheClient(root)
    spec = client.register(original).scale(width=32).png().spec
    layout = client.layout
    target = client.resolve(spec)
    ctx = multiprocessing.get_context("spawn")
    stopped, resume = ctx.Event(), ctx.Event()
    process = ctx.Process(
        target=_crash_render,
        args=(str(root), str(directory), spec.to_payload(), phase, stopped, resume),
    )
    try:
        process.start()
        assert stopped.wait(15)
        if phase in ("writing", "before_rename"):
            assert not target.exists()
        else:
            assert target.read_bytes().startswith(b"\x89PNG")
        process.kill()
        process.join(5)
        assert process.exitcode == -9
        with ManagementService(layout, directory) as service:
            assert service.catalog is not None
            service.catalog.wait_ready()
            if phase == "writing":
                assert not target.exists()
                assert not list(target.parent.glob(".*"))
                RenderWorker(layout, management=service.client).ensure(spec, retain="pin")
            else:
                assert target.read_bytes().startswith(b"\x89PNG")
            assert service.client.evict(0, 0, now=time.time() + 1000).deleted_files == 0
            assert target.exists()
            assert service.catalog.db.execute(
                'SELECT retain,state FROM artifacts WHERE kind="leaf"'
            ).fetchone()[:] == ("pin", "live")
            assert not list(target.parent.glob(".*"))
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)


def _crash_import(raw, source, mode, stopped, resume):
    store = OriginalsStore(raw)

    def pause(staged, target):
        stopped.set()
        if not resume.wait(30):
            raise RuntimeError("crash test timed out")
        os.replace(staged, target)

    patch = pytest.MonkeyPatch()
    patch.setattr(store, "_publish", pause)
    if mode == "link":
        store.link(source, file_id="a" * 32)
    else:
        store.put(source, source_file_id="a" * 32)


@pytest.mark.parametrize("mode", ["link", "register"])
def test_killed_import_leaves_only_cleanable_staging(tmp_path, mode):
    raw, source = tmp_path / "raw", tmp_path / "source"
    source.write_bytes(b"complete original")
    ctx = multiprocessing.get_context("spawn")
    stopped, resume = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_crash_import, args=(str(raw), str(source), mode, stopped, resume))
    try:
        process.start()
        assert stopped.wait(10)
        process.kill()
        process.join(5)
        assert process.exitcode == -9
        assert not (raw / ("a" * 32)).exists()
        staging = list(raw.glob(".staging-*"))
        assert len(staging) == 1
        assert (staging[0] / "source").read_bytes() == source.read_bytes()
        if mode == "link":
            assert (staging[0] / "source").stat().st_ino == source.stat().st_ino
            source.unlink()  # orphan staging now holds the last filesystem link
        assert OriginalsStore(raw).cleanup_staging(older_than=0) == 1
        assert not list(raw.iterdir())
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
