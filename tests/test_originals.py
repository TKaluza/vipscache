from __future__ import annotations

import vipscache.client
from vipscache.hash import file_id
from vipscache import VipsCacheClient
from vipscache.originals import OriginalsStore


def test_originals_store_copies_file_to_flat_hash_path(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    store = OriginalsStore(tmp_path / "raw")

    spec = store.put(source, mime="application/octet-stream")

    assert spec.file_id == file_id(source)
    assert len(spec.file_id) == 32
    assert spec.to_payload() == {
        "file_id": spec.file_id,
        "metadata": {"filename": "source.bin"},
        "mime": "application/octet-stream",
    }
    assert (tmp_path / "raw" / spec.file_id).read_bytes() == b"hello"


def test_client_register_memoizes_file_hash_until_stat_changes(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    client = VipsCacheClient(tmp_path / "shared")
    calls = 0

    with vipscache.client._INGEST_MEMO_LOCK:
        vipscache.client._INGEST_MEMO.clear()

    def counting_file_id(path):
        nonlocal calls
        calls += 1
        return file_id(path)

    monkeypatch.setattr(vipscache.client, "file_id", counting_file_id)

    first = client.register(source, mime="application/octet-stream")
    second = client.register(source, mime="application/octet-stream")
    source.write_bytes(b"hello again")
    third = client.register(source, mime="application/octet-stream")

    assert first.source.file_id == second.source.file_id
    assert third.source.file_id != first.source.file_id
    assert calls == 2


def test_async_registration_and_reads_match_sync(tmp_path):
    import asyncio

    source = tmp_path / "source.bin"
    source.write_bytes(b"sync and async API")
    client = VipsCacheClient(tmp_path / "shared")
    options = {"mime": "application/octet-stream", "metadata": {"label": "test"}}
    image = client.register(source, **options)
    assert image.read_bytes() == source.read_bytes()

    async def run():
        current = await client.aregister(source, **options)
        assert current.spec == image.spec
        assert await current.aread_bytes() == source.read_bytes()
        assert await client.aresolve(image.spec) == client.resolve(image.spec)

    asyncio.run(run())


def test_resolve_does_not_materialize_or_require_cached_file(tmp_path):
    import asyncio

    source = tmp_path / "source.bin"
    source.write_bytes(b"no renderer required")
    client = VipsCacheClient(tmp_path / "shared")
    image = client.register(source).png()
    path = client.resolve(image.spec)
    assert path == client.layout.leaf_path(image.spec.leaf.key, "png")
    assert asyncio.run(client.aresolve(image.spec)) == path
    assert not path.exists()
