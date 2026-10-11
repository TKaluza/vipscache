from __future__ import annotations

import multiprocessing
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest
from test_worker import make_image

from vipscache import (
    CacheLayout,
    ImageSpec,
    ManagementClient,
    ManagementService,
    RenderWorker,
    VipsCacheClient,
    VipsCacheError,
)
from vipscache.eviction import evict_ttl
from vipscache.executor import VipsExecutor


def test_pin_unpin_delete_restart_and_legacy_import(tmp_path, monkeypatch):
    root, local = tmp_path / "shared", tmp_path / "local"
    layout = CacheLayout(root / "cache")
    original = tmp_path / "source.ppm"
    make_image(original)
    old = (
        VipsCacheClient(root, RenderWorker(layout))
        .register(original)
        .scale(width=32)
        .materialize("pin")
        .png()
    )
    leaf = old.path()
    old_node = layout.node_path(old.spec.nodes[0].key, pinned=True)
    os.utime(leaf, (100, 100))
    old_bytes, old_mtime = leaf.read_bytes(), leaf.stat().st_mtime_ns
    with ManagementService(layout, local) as service:
        worker = RenderWorker(layout, management=service.client)
        client = VipsCacheClient(root, worker)
        pinned = old.spec
        monkeypatch.setattr(
            worker.executor, "write_leaf", lambda *args: pytest.fail("pin must not re-encode")
        )
        assert client.ensure(pinned, retain="pin") == leaf
        assert leaf.read_bytes() == old_bytes and leaf.stat().st_mtime_ns == old_mtime
        assert service.client.evict(1, 0, now=time.time() + 10000).deleted_files == 0
        assert old_node.exists()
        client.get(pinned)
        with pytest.raises(VipsCacheError, match="ManagementRequired"):
            evict_ttl(layout, 1)
    with ManagementService(layout, local) as restarted:
        worker = RenderWorker(layout, management=restarted.client)
        assert restarted.client.evict(1, 0, now=time.time() + 10000).deleted_files == 0
        worker.unpin(pinned)
        worker.unpin(pinned)  # idempotent, starts TTL only on transition
        assert restarted.client.evict(100).deleted_files == 0
        assert leaf.read_bytes() == old_bytes and leaf.stat().st_mtime_ns == old_mtime
        assert restarted.client.evict(1, 0).deleted_files == 1
        assert old_node.exists()
        worker.ensure(pinned, retain="pin")
        sibling = replace(old.spec, encode=replace(old.spec.leaf, format="webp"))
        sibling_path = worker.ensure(sibling, retain="pin")
        worker.delete([pinned])
        worker.delete([pinned])
        assert not leaf.exists() and sibling_path.exists() and old_node.exists()
    # Plain hits never need a running owner.
    client = VipsCacheClient(root)
    assert client.get(sibling) == sibling_path
    (local / "retention.sqlite3").unlink()
    with pytest.raises(VipsCacheError, match="database is missing"):
        ManagementService(layout, local)
    assert sibling_path.exists()


def test_import_budget_counts_nodes_leaves_excludes_pins_and_raw(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    keys = [str(i) * 32 for i in range(1, 5)]
    paths = [
        layout.node_path(keys[0]),
        layout.leaf_path(keys[1], "png"),
        layout.node_path(keys[2], pinned=True),
        layout.leaf_path(keys[3], "png"),
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 20)
        os.utime(path, (100 + index, 100 + index))
    with ManagementService(layout, tmp_path / "local") as service:
        report = service.client.evict(1000, 20, now=200)
        assert report.deleted_files == 2 and report.deleted_bytes == 40
        assert not paths[0].exists() and not paths[1].exists()
        assert paths[2].exists() and paths[3].exists()


def _render_process(root, socket_path, payload, started, resume, results):
    class PausingExecutor(VipsExecutor):
        def write_leaf(self, image, spec, path):
            started.set()
            if not resume.wait(15):
                raise RuntimeError("test timed out")
            super().write_leaf(image, spec, path)

    try:
        worker = RenderWorker(
            CacheLayout(Path(root) / "cache"),
            management=ManagementClient(socket_path),
            executor=PausingExecutor(),
            busy_timeout=10,
        )
        results.put(str(worker.ensure(ImageSpec.from_payload(payload), retain="pin")))
    except Exception as error:
        results.put(f"ERROR: {error}")


def test_multiple_processes_publish_pin_and_gc_share_locks(tmp_path):
    root = tmp_path / "shared"
    original = tmp_path / "source.ppm"
    make_image(original)
    source = VipsCacheClient(root).register(original).scale(width=32).materialize("force")
    specs = [source.png().spec, source.webp().spec]
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    events = [(ctx.Event(), ctx.Event()), (ctx.Event(), ctx.Event())]
    with ManagementService(CacheLayout(root / "cache"), tmp_path / "local") as service:
        children = [
            ctx.Process(
                target=_render_process,
                args=(str(root), str(service.client.path), spec.to_payload(), *events[i], results),
            )
            for i, spec in enumerate(specs)
        ]
        try:
            children[0].start()
            assert events[0][0].wait(10)
            children[1].start()
            # The first renderer already published its shared intermediate node.
            # GC skips the in-use node; the second renderer waits on the same key.
            assert service.client.evict(1, 0, now=time.time() + 1000).deleted_files == 0
            events[0][1].set()
            assert events[1][0].wait(10)
            assert service.client.evict(1, 0, now=time.time() + 1000).deleted_files == 0
            events[1][1].set()
            for child in children:
                child.join(15)
                assert child.exitcode == 0
            paths = [Path(results.get(timeout=2)) for _ in children]
            assert all(path.exists() for path in paths)
            report = service.client.evict(1, 0, now=time.time() + 1000)
            assert report.deleted_files == 1  # shared unpinned .v only
            assert all(path.exists() for path in paths)
        finally:
            for started, resume in events:
                resume.set()
            for child in children:
                if child.pid and child.is_alive():
                    child.terminate()
                    child.join()


def test_publication_and_deletion_intents_recover(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    directory = tmp_path / "local"
    key = "a" * 32
    target = layout.leaf_path(key, "png")
    target.parent.mkdir(parents=True)
    staged = target.with_name(f".{key}.crashed.png")
    rel, tmp = (p.relative_to(layout.root).as_posix() for p in (target, staged))
    with ManagementService(layout, directory) as owner:
        assert owner.catalog is not None
        owner.catalog.wait_ready()
        staged.write_bytes(b"published after restart")
        with owner.catalog.db:
            owner.catalog.db.execute(
                "INSERT INTO artifacts(path,key,kind,size,published,retain,state,staged) VALUES(?,?,'leaf',23,100,'pin','publishing',?)",
                (rel, key, tmp),
            )
    with ManagementService(layout, directory) as owner:
        assert owner.catalog is not None
        assert target.read_bytes() == b"published after restart"
        assert owner.client.evict(1, 0, now=1000).deleted_files == 0
        with owner.catalog.db:
            owner.catalog.db.execute("UPDATE artifacts SET state='deleting' WHERE path=?", (rel,))
    with ManagementService(layout, directory) as owner:
        assert owner.catalog is not None
        assert not target.exists()
        assert owner.catalog.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_second_owner_attaches_and_different_database_rejected(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    with ManagementService(layout, tmp_path / "local") as first:
        with ManagementService(layout, tmp_path / "local") as second:
            assert second.catalog is None
            assert second.client.path == first.client.path
        with pytest.raises(VipsCacheError):
            ManagementService(layout, tmp_path / "different")


def test_libvips_version_reset_of_stats_never_removes_pins(tmp_path, monkeypatch):
    from vipscache.zmq_worker import ZmqWorkerServer

    root, state = tmp_path / "shared", tmp_path / "stats"
    original = tmp_path / "source.ppm"
    make_image(original)
    spec = VipsCacheClient(root).register(original).png().spec
    with ZmqWorkerServer(
        "inproc://unused", root=root, state_dir=state, management_dir=tmp_path / "retention"
    ) as server:
        output = server.worker.ensure(spec, retain="pin")
    monkeypatch.setattr("vipscache.zmq_worker._libvips_version", lambda _: "changed-version")
    with ZmqWorkerServer(
        "inproc://unused", root=root, state_dir=state, management_dir=tmp_path / "retention"
    ) as server:
        assert server.worker.management is not None
        assert server.worker.management.evict(1, 0, now=time.time() + 1000).deleted_files == 0
        assert output.exists()
    with pytest.raises(ValueError, match="outside disposable"):
        ZmqWorkerServer(
            "inproc://unused", root=root, state_dir=state, management_dir=state / "retention"
        )


def test_stale_gc_candidate_cannot_delete_a_new_pin(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    path = tmp_path / "source.ppm"
    make_image(path)
    layout = CacheLayout(root / "cache")
    with ManagementService(layout, tmp_path / "local") as service:
        catalog = service.catalog
        assert catalog is not None
        worker = RenderWorker(layout, management=service.client)
        spec = VipsCacheClient(root).register(path).png().spec
        output = worker.materialize(spec)
        scanned, resume = Event(), Event()
        original_locked = catalog.locked

        @contextmanager
        def paused(keys, timeout=2.0, **kwargs):
            if current_thread().name == "gc-test":
                scanned.set()
                assert resume.wait(5)
            with original_locked(keys, timeout, **kwargs):
                yield

        monkeypatch.setattr(service.catalog, "locked", paused)
        results = []
        cleanup = Thread(
            name="gc-test",
            target=lambda: results.append(catalog.evict(1, 0, now=time.time() + 1000)),
        )
        cleanup.start()
        try:
            assert scanned.wait(5)
            worker.ensure(spec, retain="pin")
        finally:
            resume.set()
            cleanup.join(5)
        assert results[0].deleted_files == 0
        assert output.exists()


def test_periodic_cleanup_runs_without_request(tmp_path):
    from vipscache.zmq_worker import ZmqWorkerServer

    layout = CacheLayout(tmp_path / "shared" / "cache")
    path = layout.leaf_path("a" * 32, "png")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"legacy leaf")
    os.utime(path, (100, 100))
    server = ZmqWorkerServer(
        f"ipc://{tmp_path}/worker.sock",
        root=layout.root.parent,
        ttl_seconds=1,
        gc_interval_seconds=0.02,
        management_dir=tmp_path / "retention",
    )
    serving = Thread(target=server.serve_forever)
    serving.start()
    try:
        deadline = time.monotonic() + 5
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not path.exists()
    finally:
        server._running.clear()
        serving.join(5)
        server.close()
