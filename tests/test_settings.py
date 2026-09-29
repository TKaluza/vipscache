from __future__ import annotations

from vipscache.settings import WorkerSettings


def test_worker_settings_reads_prefixed_environment(monkeypatch):
    monkeypatch.setenv("VIPSCACHE_MAX_WORKERS", "6")
    monkeypatch.setenv("VIPSCACHE_TTL_SECONDS", "123")
    monkeypatch.setenv("VIPSCACHE_LIBVIPS_CACHE_MAX_MEM_MB", "64")
    monkeypatch.setenv("VIPSCACHE_STATE_DIR", "/state")
    monkeypatch.setenv("VIPSCACHE_STATE_MAP_SIZE_MB", "32")
    monkeypatch.setenv("VIPSCACHE_BUSY_TIMEOUT_SECONDS", "1.5")

    settings = WorkerSettings()

    assert settings.max_workers == 6
    assert settings.ttl_seconds == 123
    assert settings.libvips_cache_max_mem_mb == 64
    assert settings.libvips_cache_max_ops == 0
    assert str(settings.state_dir) == "/state"
    assert settings.state_map_size_mb == 32
    assert settings.busy_timeout_seconds == 1.5


def test_worker_settings_uses_root_for_storage(monkeypatch):
    monkeypatch.setenv("VIPSCACHE_ROOT", "/shared")

    settings = WorkerSettings()

    assert str(settings.root) == "/shared"
