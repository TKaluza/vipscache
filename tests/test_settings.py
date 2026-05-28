from __future__ import annotations

from imgcache.settings import WorkerSettings


def test_worker_settings_reads_prefixed_environment(monkeypatch):
    monkeypatch.setenv("IMGCACHE_MAX_WORKERS", "6")
    monkeypatch.setenv("IMGCACHE_TTL_SECONDS", "123")
    monkeypatch.setenv("IMGCACHE_LIBVIPS_CACHE_MAX_MEM_MB", "64")

    settings = WorkerSettings()

    assert settings.max_workers == 6
    assert settings.ttl_seconds == 123
    assert settings.libvips_cache_max_mem_mb == 64
    assert settings.libvips_cache_max_ops == 0
