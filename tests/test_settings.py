from __future__ import annotations

from imgcache.settings import WorkerSettings


def test_worker_settings_reads_prefixed_environment(monkeypatch):
    monkeypatch.setenv("IMGCACHE_MAX_WORKERS", "6")
    monkeypatch.setenv("IMGCACHE_TTL_SECONDS", "123")
    monkeypatch.setenv("IMGCACHE_LIBVIPS_CACHE_MAX_MEM_MB", "64")
    monkeypatch.setenv("IMGCACHE_STATE_DIR", "/state")
    monkeypatch.setenv("IMGCACHE_STATE_MAP_SIZE_MB", "32")
    monkeypatch.setenv("IMGCACHE_BUSY_TIMEOUT_SECONDS", "1.5")

    settings = WorkerSettings()

    assert settings.max_workers == 6
    assert settings.ttl_seconds == 123
    assert settings.libvips_cache_max_mem_mb == 64
    assert settings.libvips_cache_max_ops == 0
    assert str(settings.state_dir) == "/state"
    assert settings.state_map_size_mb == 32
    assert settings.busy_timeout_seconds == 1.5


def test_worker_settings_uses_root_for_storage(monkeypatch):
    monkeypatch.setenv("IMGCACHE_ROOT", "/shared")

    settings = WorkerSettings()

    assert str(settings.root) == "/shared"
