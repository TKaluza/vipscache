from __future__ import annotations

from vipscache.settings import WorkerSettings


def test_worker_settings_reads_prefixed_environment(monkeypatch):
    monkeypatch.setenv("VIPSCACHE_MAX_WORKERS", "6")
    monkeypatch.setenv("VIPSCACHE_TTL_SECONDS", "123")
    monkeypatch.setenv("VIPSCACHE_LIBVIPS_CACHE_MAX_MEM_MB", "64")
    monkeypatch.setenv("VIPSCACHE_STATE_DIR", "/state")
    monkeypatch.setenv("VIPSCACHE_STATE_MAP_SIZE_MB", "32")
    monkeypatch.setenv("VIPSCACHE_BUSY_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("VIPSCACHE_MANAGEMENT_DIR", "/retention")
    monkeypatch.setenv("VIPSCACHE_MANAGEMENT_SESSION_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("VIPSCACHE_GC_INTERVAL_SECONDS", "12")
    monkeypatch.setenv("VIPSCACHE_MAX_CACHE_BYTES", "123456")
    monkeypatch.setenv("VIPSCACHE_MAX_INPUT_PIXELS", "1000")
    monkeypatch.setenv("VIPSCACHE_MAX_OUTPUT_PIXELS", "2000")
    monkeypatch.setenv("VIPSCACHE_MAX_DPI", "300")

    settings = WorkerSettings()

    assert settings.max_workers == 6
    assert settings.ttl_seconds == 123
    assert settings.libvips_cache_max_mem_mb == 64
    assert settings.libvips_cache_max_ops == 0
    assert str(settings.state_dir) == "/state"
    assert settings.state_map_size_mb == 32
    assert settings.busy_timeout_seconds == 1.5
    assert str(settings.management_dir) == "/retention"
    assert settings.management_session_timeout_seconds == 900
    assert settings.gc_interval_seconds == 12
    assert settings.max_cache_bytes == 123456
    assert settings.max_input_pixels == 1000
    assert settings.max_output_pixels == 2000
    assert settings.max_dpi == 300


def test_worker_settings_uses_root_for_storage(monkeypatch):
    monkeypatch.setenv("VIPSCACHE_ROOT", "/shared")

    settings = WorkerSettings()

    assert str(settings.root) == "/shared"
