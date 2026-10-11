from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Worker configuration loaded from VIPSCACHE-prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="VIPSCACHE_")

    endpoint: str = "tcp://*:5555"
    root: Path = Path("/data")
    max_workers: int = Field(default=4, ge=1)
    ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1)
    state_dir: Path | None = None
    state_map_size_mb: int = Field(default=1024, ge=1)
    busy_timeout_seconds: float = Field(default=2.0, gt=0)
    management_dir: Path | None = None
    management_session_timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    gc_interval_seconds: float = Field(default=60.0, ge=0)
    max_cache_bytes: int | None = Field(default=None, ge=0)
    max_input_pixels: int | None = Field(default=None, ge=1)
    max_output_pixels: int | None = Field(default=None, ge=1)
    max_dpi: int | None = Field(default=None, ge=1)

    libvips_concurrency: int = Field(default=1, ge=1)
    libvips_cache_max_mem_mb: int = Field(default=128, ge=0)
    libvips_cache_max_files: int = Field(default=100, ge=0)
    libvips_cache_max_ops: int = Field(default=0, ge=0)


def configure_libvips(settings: WorkerSettings) -> None:
    os.environ.setdefault("VIPS_CONCURRENCY", str(settings.libvips_concurrency))

    try:
        import pyvips
    except ImportError as error:
        raise RuntimeError("Worker settings require pyvips; install vipscache[worker].") from error

    pyvips.cache_set_max_mem(settings.libvips_cache_max_mem_mb * 1024 * 1024)
    pyvips.cache_set_max_files(settings.libvips_cache_max_files)
    pyvips.cache_set_max(settings.libvips_cache_max_ops)
