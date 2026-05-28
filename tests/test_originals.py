from __future__ import annotations

from imgcache.hash import file_id
from imgcache.originals import OriginalsStore


def test_originals_store_copies_file_to_flat_hash_path(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    store = OriginalsStore(tmp_path / "raw")

    spec = store.put(source, mime="application/octet-stream")

    assert spec.file_id == file_id(source)
    assert spec.to_payload() == {
        "file_id": spec.file_id,
        "metadata": {"filename": "source.bin"},
        "mime": "application/octet-stream",
    }
    assert (tmp_path / "raw" / spec.file_id).read_bytes() == b"hello"
