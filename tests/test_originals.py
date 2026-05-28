from __future__ import annotations

from imgcache.hash import file_id
from imgcache.originals import OriginalsStore


def test_originals_store_copies_file_to_flat_hash_path(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    store = OriginalsStore(tmp_path / "originals")

    spec = store.put(source, mime="application/octet-stream")

    assert spec.file_id == file_id(source)
    assert spec.original_path == (tmp_path / "originals" / spec.file_id).as_posix()
    assert (tmp_path / "originals" / spec.file_id).read_bytes() == b"hello"
