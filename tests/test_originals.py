from __future__ import annotations

import imgcache.client
from imgcache.hash import file_id
from imgcache import ImgCacheClient
from imgcache.originals import OriginalsStore


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
    client = ImgCacheClient(tmp_path / "shared")
    calls = 0

    with imgcache.client._INGEST_MEMO_LOCK:
        imgcache.client._INGEST_MEMO.clear()

    def counting_file_id(path):
        nonlocal calls
        calls += 1
        return file_id(path)

    monkeypatch.setattr(imgcache.client, "file_id", counting_file_id)

    first = client.register(source, mime="application/octet-stream")
    second = client.register(source, mime="application/octet-stream")
    source.write_bytes(b"hello again")
    third = client.register(source, mime="application/octet-stream")

    assert first.source.file_id == second.source.file_id
    assert third.source.file_id != first.source.file_id
    assert calls == 2


def test_python_legacy_aliases_preserve_registration_and_reads(tmp_path):
    import asyncio

    source = tmp_path / "source.bin"
    source.write_bytes(b"legacy and current API")
    client = ImgCacheClient(tmp_path / "shared")
    options = {"mime": "application/octet-stream", "metadata": {"label": "test"}}
    image = client.register(source, **options)
    legacy = client.open(source, **options)
    assert legacy.spec == image.spec
    assert legacy.bytes() == image.read_bytes() == source.read_bytes()
    assert client.path_for(image.spec) == client.resolve(image.spec)

    async def run():
        current = await client.aregister(source, **options)
        old = await client.aopen(source, **options)
        assert current.spec == old.spec == image.spec
        assert await old.abytes() == await current.aread_bytes() == source.read_bytes()
        assert await client.aresolve(image.spec) == client.resolve(image.spec)

    asyncio.run(run())


def test_resolve_does_not_materialize_or_require_cached_file(tmp_path):
    import asyncio

    source = tmp_path / "source.bin"
    source.write_bytes(b"no renderer required")
    client = ImgCacheClient(tmp_path / "shared")
    image = client.register(source).png()
    path = client.resolve(image.spec)
    assert path == client.layout.leaf_path(image.spec.leaf.key, "png")
    assert asyncio.run(client.aresolve(image.spec)) == path
    assert not path.exists()
