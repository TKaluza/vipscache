from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from vipscache import ErrorType, VipsCacheClient, VipsCacheError
from vipscache.originals import OriginalsStore


@pytest.mark.parametrize("mode", ["link", "register"])
def test_concurrent_cleanup_reports_staging_lost_without_retry(tmp_path, monkeypatch, mode):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    client = VipsCacheClient(tmp_path / "shared")
    publish = client.raw._publish
    calls = []

    def cleanup_before_publish(staged, target):
        calls.append(staged)
        assert client.cleanup_staging(older_than=0) == 1
        publish(staged, target)

    monkeypatch.setattr(client.raw, "_publish", cleanup_before_publish)
    with pytest.raises(VipsCacheError) as caught:
        getattr(client, mode)(source, file_id="a" * 32)
    assert caught.value.type == ErrorType.STAGING_LOST
    assert len(calls) == 1
    assert source.read_bytes() == b"original"
    assert not list(client.raw.root.iterdir())
    with pytest.raises(VipsCacheError) as missing:
        client.raw.put(tmp_path / "absent", source_file_id="a" * 32)
    assert missing.value.type == ErrorType.ORIGINAL_MISSING


def test_cleanup_uses_staging_age_and_preserves_unknown_entries(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"old source")
    os.utime(source, (100, 100))
    current = raw / f".staging-{uuid.uuid4()}"
    current.mkdir()
    os.link(source, current / "source")
    old = raw / f".staging-{uuid.uuid4()}"
    old.mkdir()
    os.link(source, old / "source")
    os.utime(old, (100, 100))
    legacy_directory = raw / ("." + "a" * 32 + ".abcdefgh")
    legacy_directory.mkdir()
    os.link(source, legacy_directory / "source")
    os.utime(legacy_directory, (100, 100))
    # A legacy Node hardlink has an old mtime, but a fresh link-count ctime.
    legacy_link = raw / f".{uuid.uuid4()}"
    os.link(source, legacy_link)
    foreign = raw / ".foreign"
    foreign.write_bytes(b"keep")
    os.utime(foreign, (100, 100))
    canonical = raw / ("a" * 32)
    os.link(source, canonical)
    unexpected = raw / f".staging-{uuid.uuid4()}"
    unexpected.mkdir()
    (unexpected / "unrelated").write_bytes(b"keep")
    os.utime(unexpected, (100, 100))
    store = OriginalsStore(raw)
    assert store.cleanup_staging(older_than=3600) == 2
    assert current.exists() and legacy_link.exists()
    assert canonical.read_bytes() == b"old source"
    assert foreign.read_bytes() == b"keep"
    assert unexpected.exists()
    # Once legacy entries really are old, their recognized names can be cleaned.
    assert store.cleanup_staging(older_than=3600, now=time.time() + 7200) == 2
    assert not current.exists() and not legacy_link.exists()
    assert foreign.exists() and canonical.exists() and unexpected.exists()


def test_cleanup_does_not_follow_staging_symlinks(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "important").write_bytes(b"keep")
    modern = raw / f".staging-{uuid.uuid4()}"
    modern.symlink_to(external)
    legacy = raw / f".{uuid.uuid4()}"
    legacy.symlink_to(external)
    assert OriginalsStore(raw).cleanup_staging(older_than=0) == 1
    assert modern.is_symlink()
    assert (external / "important").read_bytes() == b"keep"


def test_async_cleanup_and_invalid_age(tmp_path):
    client = VipsCacheClient(tmp_path / "shared")
    assert asyncio.run(client.acleanup_staging()) == 0
    for age in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            client.cleanup_staging(older_than=age)
