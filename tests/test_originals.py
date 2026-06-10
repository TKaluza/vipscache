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


def test_client_open_memoizes_file_hash_until_stat_changes(tmp_path, monkeypatch):
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

    first = client.open(source, mime="application/octet-stream")
    second = client.open(source, mime="application/octet-stream")
    source.write_bytes(b"hello again")
    third = client.open(source, mime="application/octet-stream")

    assert first.source.file_id == second.source.file_id
    assert third.source.file_id != first.source.file_id
    assert calls == 2
