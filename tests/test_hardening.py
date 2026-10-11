from __future__ import annotations

import asyncio
import multiprocessing
import os
import pickle
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest
from test_management import _render_process
from test_worker import make_image

from vipscache import (
    CacheLayout,
    ErrorType,
    ManagementClient,
    ManagementService,
    RenderWorker,
    VipsCacheClient,
    VipsCacheError,
)
from vipscache.catalog import Catalog
from vipscache.errors import (
    BusyError,
    InvalidSpecError,
    LimitExceededError,
    RequestTimeoutError,
    error_payload,
)
from vipscache.zmq_worker import ZmqWorkerServer


def image_client(tmp_path, management=None):
    root = tmp_path / "shared"
    path = tmp_path / "source.ppm"
    make_image(path)
    worker = RenderWorker(CacheLayout(root / "cache"), management=management)
    client = VipsCacheClient(root, worker)
    image = client.register(path).scale(width=32).materialize("force").png()
    return client, worker, image


def test_read_hits_survive_missing_nodes_raw_and_owner(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    with ManagementService(layout, tmp_path / "retention") as service:
        client, worker, image = image_client(tmp_path, service.client)
        leaf = image.ensure(retain="pin")
        contents = leaf.read_bytes()
        node = layout.node_path(image.spec.nodes[0].key)
        node.unlink()
        # No rebuilding of expired intermediates on direct reads.
        assert image.path() == leaf
        assert not node.exists()
        client.unlink(image.source.file_id)
        assert image.read_bytes() == contents
        with pytest.raises(VipsCacheError, match="OriginalMissing"):
            image.ensure(retain="pin")
    client.worker = None
    assert image.path() == leaf
    with image.open() as stream:
        assert stream.read() == contents

    async def read():
        assert await image.apath() == leaf
        assert await image.aread_bytes() == contents
        with await image.aopen() as stream:
            assert stream.read() == contents

    asyncio.run(read())


def test_ensure_and_management_are_explicit_opt_in(tmp_path, monkeypatch):
    home = tmp_path / "unexpected-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    with ZmqWorkerServer(
        "inproc://unused", root=tmp_path / "shared", state_dir=tmp_path / "stats"
    ) as server:
        assert server.worker.management is None
        client = VipsCacheClient(tmp_path / "shared", server.worker)
        source = tmp_path / "image.ppm"
        make_image(source)
        image = client.register(source).scale(width=32).png()
        leaf = image.path()
        requested = client.register(source).scale(width=32).materialize("pin").png()
        assert requested.ensure() == leaf
        assert client.layout.node_path(requested.spec.nodes[0].key, pinned=True).exists()
        with pytest.raises(VipsCacheError, match="ManagementUnavailable"):
            requested.ensure(retain="pin")
        assert requested.path() == leaf
        assert not client.layout.management_marker.exists()
        assert not home.exists()
        with pytest.raises(ValueError, match="ensure"):
            image.webp(retain="pin")


def test_missing_unpin_and_bidirectional_reconcile(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    directory = tmp_path / "retention"
    with ManagementService(layout, directory) as service:
        assert service.catalog is not None
        client, worker, image = image_client(tmp_path, service.client)
        path = image.ensure(retain="pin")
        path.unlink()
        image.unpin()
        row = service.catalog.db.execute(
            "SELECT retain,state FROM artifacts WHERE path=?",
            (path.relative_to(layout.root).as_posix(),),
        ).fetchone()
        assert tuple(row) == ("ttl", "missing")
        # A subsequent normal read/miss must not resurrect the old pin.
        image.path()
        assert (
            service.catalog.db.execute('SELECT retain FROM artifacts WHERE kind="leaf"').fetchone()[
                0
            ]
            == "ttl"
        )
        image.ensure(retain="pin")
        path.unlink()
    with ManagementService(layout, directory) as restarted:
        assert restarted.catalog is not None
        restarted.catalog.wait_ready()
        assert restarted.catalog.db.execute(
            'SELECT retain,state,size FROM artifacts WHERE kind="leaf"'
        ).fetchone()[:] == ("pin", "missing", 0)
        worker = RenderWorker(layout, management=restarted.client)
        worker.materialize(image.spec)
        assert restarted.catalog.db.execute(
            'SELECT retain,state FROM artifacts WHERE kind="leaf"'
        ).fetchone()[:] == ("pin", "live")


def test_missing_newer_file_does_not_evict_live_older_file(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    paths = [layout.leaf_path(c * 32, "png") for c in ("a", "b")]
    for timestamp, path in zip((100, 200), paths):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 20)
        os.utime(path, (timestamp, timestamp))
    with ManagementService(layout, tmp_path / "retention") as service:
        assert service.catalog is not None
        service.catalog.wait_ready()
    paths[1].unlink()
    with ManagementService(layout, tmp_path / "retention") as service:
        assert service.catalog is not None
        report = service.client.evict(1000, 20, now=300)
        assert report.deleted_files == 0
        assert paths[0].exists()
        assert service.catalog.db.execute("SELECT ttl_bytes FROM usage").fetchone()[0] == 20
        assert service.catalog.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1


def test_background_reconcile_allows_writes_but_gates_cleanup(tmp_path, monkeypatch):
    started, resume = Event(), Event()
    reconcile = Catalog._reconcile

    def paused(self):
        started.set()
        assert resume.wait(10)
        reconcile(self)

    monkeypatch.setattr(Catalog, "_reconcile", paused)
    layout = CacheLayout(tmp_path / "shared" / "cache")
    with ManagementService(layout, tmp_path / "retention") as service:
        assert service.catalog is not None
        try:
            assert started.wait(2)
            assert not service.client.request({"method": "health"})["ready"]
            client, worker, image = image_client(tmp_path, service.client)
            path = image.ensure(retain="pin")
            other = client.register(tmp_path / "source.ppm").jpg()
            other.ensure()
            other.delete()
            with pytest.raises(BusyError):
                service.catalog.wait_ready(timeout=0.01)
            assert path.exists()
        finally:
            resume.set()
        service.catalog.wait_ready()
        assert not client.resolve(other.spec).exists()
        assert (
            service.catalog.db.execute(
                'SELECT count(*) FROM artifacts WHERE kind="leaf"'
            ).fetchone()[0]
            == 1
        )
        assert (
            service.client.evict(1, 0, now=time.time() + 1000).deleted_files == 1
        )  # only the force node
        assert path.exists()


def test_parallel_processes_read_shared_parent_and_gc_waits(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    ctx = multiprocessing.get_context("spawn")
    with ManagementService(layout, tmp_path / "retention") as service:
        client, worker, image = image_client(tmp_path, service.client)
        image.ensure()
        image.delete()
        base = client.register(tmp_path / "source.ppm").scale(width=32).materialize("force")
        specs = [base.jpg().spec, base.webp().spec]
        events = [(ctx.Event(), ctx.Event()), (ctx.Event(), ctx.Event())]
        results = ctx.Queue()
        children = [
            ctx.Process(
                target=_render_process,
                args=(
                    str(client.root),
                    str(service.client.path),
                    spec.to_payload(),
                    *events[i],
                    results,
                ),
            )
            for i, spec in enumerate(specs)
        ]
        try:
            for child in children:
                child.start()
            # Both reach leaf encoding before either is allowed to finish.
            assert all(started.wait(10) for started, _ in events)
            assert service.client.evict(1, 0, now=time.time() + 1000).deleted_files == 0
            for _, resume in events:
                resume.set()
            for child in children:
                child.join(10)
                assert child.exitcode == 0
            assert all(Path(results.get(timeout=2)).exists() for _ in children)
        finally:
            for _, resume in events:
                resume.set()
            for child in children:
                if child.is_alive():
                    child.kill()
                child.join(5)
            results.close()


def test_lock_recheck_retries_once_pessimistically(tmp_path, monkeypatch):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    with ManagementService(layout, tmp_path / "retention") as service:
        client, worker, image = image_client(tmp_path, service.client)
        image.ensure()
        node = layout.node_path(image.spec.nodes[0].key)
        key = node.stem
        original = service.client.session
        requests = []

        @contextmanager
        def raced(keys, timeout=2.0, **kwargs):
            requests.append((set(keys), timeout))
            if len(requests) == 1:
                with original([key]) as session:
                    session.request(
                        {"method": "delete", "path": node.relative_to(layout.root).as_posix()}
                    )
            with original(keys, timeout, **kwargs) as session:
                yield session

        monkeypatch.setattr(service.client, "session", raced)
        image.ensure()
        assert node.exists()
        assert len(requests) == 2
        assert key not in requests[0][0] and key in requests[1][0]
        assert requests[1][1] <= requests[0][1]


def test_management_transport_timeout_and_bounded_close(tmp_path):
    socket_path = tmp_path / "stalled.sock"
    release = Event()
    accepted = Event()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen()

        def stalled():
            with listener.accept()[0]:
                accepted.set()
                release.wait(5)

        thread = Thread(target=stalled)
        thread.start()
        try:
            start = time.monotonic()
            with pytest.raises(VipsCacheError, match="ManagementUnavailable"):
                ManagementClient(socket_path, timeout=0.05).request({"method": "health"})
            assert time.monotonic() - start < 1
        finally:
            release.set()
            thread.join(2)
    service = ManagementService(
        CacheLayout(tmp_path / "shared" / "cache"), tmp_path / "retention", shutdown_timeout=0.5
    )
    assert service.catalog is not None
    scope = service.client.session(["a" * 32])
    scope.__enter__()
    start = time.monotonic()
    service.close()
    assert time.monotonic() - start < 1
    with pytest.raises(VipsCacheError, match="ManagementUnavailable"):
        scope.__exit__(None, None, None)
    with ManagementService(service.catalog.layout, tmp_path / "retention") as restarted:
        with restarted.client.session(["a" * 32]):
            pass


@pytest.mark.parametrize(
    "error",
    [
        VipsCacheError(ErrorType.STAGING_LOST, "lost"),
        BusyError(),
        InvalidSpecError("bad"),
        LimitExceededError("large"),
        RequestTimeoutError("late"),
        VipsCacheError("FutureError", "future"),
    ],
)
def test_errors_pickle_without_prefix_duplication(error):
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is type(error)
    assert restored.type == error.type
    assert str(restored) == str(error)
    assert error_payload(restored)["message"] == error.message


def test_unknown_exceptions_are_worker_errors(tmp_path, monkeypatch):
    for error in (
        FileNotFoundError("node"),
        ValueError("internal bug"),
        TypeError("internal bug"),
        KeyError("internal bug"),
    ):
        assert error_payload(error)["type"] == ErrorType.WORKER_ERROR
    with ZmqWorkerServer("inproc://unused", root=tmp_path / "shared") as server:

        def broken_render(spec):
            raise ValueError("internal bug")

        monkeypatch.setattr(server.worker, "materialize", broken_render)
        spec = {"source": {"file_id": "a" * 32}, "encode": {"format": "png"}}
        assert (
            server._handle_request({"method": "materialize", "spec": spec})["error"]["type"]
            == ErrorType.WORKER_ERROR
        )
        assert (
            server._handle_request({"method": "materialize", "spec": {}})["error"]["type"]
            == ErrorType.INVALID_SPEC
        )


def test_idle_budget_gc_does_not_walk_catalog(tmp_path):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    with ManagementService(layout, tmp_path / "retention") as service:
        assert service.catalog is not None
        service.catalog.wait_ready()
        rows = [
            (f"leaves/{key[:2]}/{key}.png", key, 1000)
            for key in (f"{i:032x}" for i in range(10000))
        ]
        with service.catalog.db:
            service.catalog.db.executemany(
                "INSERT INTO artifacts(path,key,kind,size,published,retain) VALUES(?,?,'leaf',1,?,'ttl')",
                rows,
            )
        queries = []
        service.catalog.db.set_trace_callback(queries.append)
        try:
            assert service.catalog.evict(100, 10000, now=1000).deleted_files == 0
        finally:
            service.catalog.db.set_trace_callback(None)
        assert len(queries) < 10
        assert not any("SELECT * FROM artifacts WHERE path=" in query for query in queries)


def test_renewed_first_gc_candidate_does_not_skip_other_expired_files(tmp_path, monkeypatch):
    layout = CacheLayout(tmp_path / "shared" / "cache")
    paths = [layout.leaf_path(c * 32, "png") for c in ("a", "b")]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"cached")
        os.utime(path, (100 + index, 100 + index))
    with ManagementService(layout, tmp_path / "retention") as service:
        catalog = service.catalog
        assert catalog is not None
        catalog.wait_ready()
        locked = catalog.locked
        renewed = False

        @contextmanager
        def renew_first(keys, timeout=2.0, **kwargs):
            nonlocal renewed
            if not renewed:
                renewed = True
                with catalog.db:
                    catalog.db.execute(
                        "UPDATE artifacts SET published=1000 WHERE key=?", ("a" * 32,)
                    )
            with locked(keys, timeout, **kwargs):
                yield

        monkeypatch.setattr(service.catalog, "locked", renew_first)
        assert catalog.evict(100, 100, now=500).deleted_files == 1
        assert paths[0].exists() and not paths[1].exists()
