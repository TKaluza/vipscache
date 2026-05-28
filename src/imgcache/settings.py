from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IMGCACHE_")

    endpoint: str = "tcp://*:5555"
    root: Path = Path("/data")
    cache_root: Path | None = None
    raw_root: Path | None = None
    originals_root: Path | None = None
    max_workers: int = Field(default=4, ge=1)
    ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1)

    libvips_concurrency: int = Field(default=1, ge=1)
    libvips_cache_max_mem_mb: int = Field(default=128, ge=0)
    libvips_cache_max_files: int = Field(default=100, ge=0)
    libvips_cache_max_ops: int = Field(default=0, ge=0)

    @property
    def effective_cache_root(self) -> Path:
        return self.cache_root or self.root / "cache"

    @property
    def effective_raw_root(self) -> Path:
        return self.raw_root or self.originals_root or self.root / "raw"


def configure_libvips(settings: WorkerSettings) -> None:
    os.environ.setdefault("VIPS_CONCURRENCY", str(settings.libvips_concurrency))

    try:
        import pyvips
    except ImportError as error:
        raise RuntimeError("Worker settings require pyvips; install imgcache[worker].") from error

    pyvips.cache_set_max_mem(settings.libvips_cache_max_mem_mb * 1024 * 1024)
    pyvips.cache_set_max_files(settings.libvips_cache_max_files)
    pyvips.cache_set_max(settings.libvips_cache_max_ops)
