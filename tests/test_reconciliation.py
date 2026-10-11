"""Exercise write-free restarts and bounded catalog import transactions."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_worker import make_image

from vipscache import CacheLayout, ManagementService, RenderWorker, VipsCacheClient
from vipscache.catalog import Catalog
from vipscache.executor import VipsExecutor
from vipscache.zmq_worker import ZmqWorkerServer


def seed_leaves(layout: CacheLayout, count: int) -> list[Path]:
    paths = [layout.leaf_path(f"{index:032x}", "png") for index in range(count)]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached")
        os.utime(path, (100, 100))
    return paths


def test_unchanged_restart_does_not_write_artifacts(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    paths = seed_leaves(layout, 17)
    directory = tmp_path / "retention"
    catalog = Catalog(layout, directory)
    catalog._reconcile()
    # Include a durable missing pin: a restart must not keep bumping its revision.
    with catalog.db:
        catalog.db.execute("UPDATE artifacts SET retain='pin' WHERE key=?", (paths[0].stem,))
    paths[0].unlink()
    catalog._reconcile()
    before = catalog.db.execute("SELECT * FROM artifacts ORDER BY path").fetchall()
    catalog.close()

    restarted = Catalog(layout, directory)
    try:
        queries: list[str] = []
        changes = restarted.db.total_changes
        restarted.db.set_trace_callback(queries.append)
        restarted._reconcile()
        assert restarted.scan_error is None
        assert restarted.db.total_changes == changes
        assert restarted.db.execute("SELECT * FROM artifacts ORDER BY path").fetchall() == before
        assert not any(
            query.startswith(("INSERT", "UPDATE", "DELETE", "COMMIT")) for query in queries
        )
    finally:
        restarted.close()


def test_import_commits_in_batches_and_reconciles_only_changes(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    paths = seed_leaves(layout, 300)
    catalog = Catalog(layout, tmp_path / "retention")
    queries: list[str] = []
    try:
        catalog.db.set_trace_callback(queries.append)
        catalog._reconcile()
        assert catalog.scan_error is None
        assert sum(query == "COMMIT" for query in queries) == 3
        assert catalog.db.execute("SELECT ttl_bytes FROM usage").fetchone()[0] == 1800
        paths[0].write_bytes(b"changed-size")
        paths[1].unlink()
        queries.clear()
        catalog._reconcile()
        assert catalog.scan_error is None
        assert catalog.db.execute("SELECT ttl_bytes FROM usage").fetchone()[0] == 1800
        assert catalog.db.execute(
            "SELECT revision,published FROM artifacts WHERE key=?", (paths[0].stem,)
        ).fetchone()[:] == (2, 100)
        assert (
            catalog.db.execute("SELECT count(*) FROM artifacts WHERE revision>1").fetchone()[0] == 1
        )
        assert catalog.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 299
        assert sum(query == "COMMIT" for query in queries) == 2
    finally:
        catalog.close()


@pytest.mark.parametrize("old_index", [False, True])
def test_gc_uses_ordered_index_after_catalog_restart(tmp_path, old_index):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    directory = tmp_path / "retention"
    paths = seed_leaves(layout, 300)
    catalog = Catalog(layout, directory)
    try:
        catalog._reconcile()
        with catalog.db:
            catalog.db.execute("UPDATE artifacts SET retain='pin' WHERE key=?", (paths[0].stem,))
            if old_index:
                catalog.db.execute("CREATE INDEX expiry ON artifacts(retain, published)")
        before = catalog.db.execute("SELECT * FROM artifacts ORDER BY path").fetchall()
    finally:
        catalog.close()

    restarted = Catalog(layout, directory)
    try:
        restarted._reconcile()
        indexes = {row["name"] for row in restarted.db.execute("PRAGMA index_list(artifacts)")}
        assert "expiry" not in indexes
        assert "gc_live" in indexes
        assert restarted.db.execute("SELECT * FROM artifacts ORDER BY path").fetchall() == before
        queries: list[str] = []
        restarted.db.set_trace_callback(queries.append)
        assert restarted.evict(100, now=150).deleted_files == 0
        restarted.db.set_trace_callback(None)
        candidates = [query for query in queries if "ORDER BY published,path LIMIT" in query]
        assert len(candidates) == 1
        # SQLite's trace spells bound infinity as Inf, which is not a SQL literal.
        query = candidates[0].replace("-Inf", "?")
        plan = [
            row["detail"]
            for row in restarted.db.execute("EXPLAIN QUERY PLAN " + query, (-float("inf"),))
        ]
        assert any("gc_live" in detail for detail in plan)
        assert not any("TEMP B-TREE" in detail for detail in plan)
        report = restarted.evict(100, now=300)
        assert report.deleted_files == 299
        assert paths[0].exists()
        assert restarted.db.execute("SELECT ttl_bytes FROM usage").fetchone()[0] == 0
    finally:
        restarted.close()


@pytest.mark.parametrize("budget", [0, 1024])
def test_budget_requires_management_before_creating_storage(tmp_path, budget):
    root = tmp_path / "shared"
    with pytest.raises(ValueError, match="max_cache_bytes requires management_dir"):
        ZmqWorkerServer("inproc://unused", root=root, max_cache_bytes=budget)
    assert not root.exists()


def test_budget_accepts_an_injected_management_client(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    with ManagementService(layout, tmp_path / "retention") as service:
        worker = RenderWorker(layout, management=service.client)
        with ZmqWorkerServer("inproc://unused", worker=worker, max_cache_bytes=0) as server:
            assert server.worker.management is service.client


def test_configured_session_timeout_allows_slow_publication(tmp_path):
    class SlowExecutor(VipsExecutor):
        def write_leaf(self, image, spec, path):
            time.sleep(0.15)
            super().write_leaf(image, spec, path)

    root = tmp_path / "shared"
    source = tmp_path / "source.ppm"
    make_image(source)
    worker = RenderWorker(CacheLayout(root / "cache"), executor=SlowExecutor())
    with ZmqWorkerServer(
        "inproc://unused",
        worker=worker,
        management_dir=tmp_path / "retention",
        management_session_timeout_seconds=1.0,
    ) as server:
        service = server._management_service
        assert service is not None and service._server is not None
        assert service._server.session_timeout == 1.0
        path = VipsCacheClient(root, worker).register(source).png().ensure(retain="pin")
        assert path.read_bytes().startswith(b"\x89PNG")
