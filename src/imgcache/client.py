from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, Self

from imgcache.hash import file_id
from imgcache.io import open_cache_hit
from imgcache.layout import CacheLayout
from imgcache.originals import OriginalsStore
from imgcache.spec import EncodeSpec, ImageSpec, Operation, SourceSpec


TRANSFORMED_WITHOUT_ENCODE = (
    "transformed CachedImage must choose an output format; call .webp(), .png(), or .jpg()"
)
_INGEST_MEMO_MAX = 4096
_INGEST_MEMO_LOCK = Lock()
_INGEST_MEMO: OrderedDict[tuple[Path, int, int], str] = OrderedDict()


class WorkerClient(Protocol):
    def materialize(self, spec: ImageSpec) -> Path:
        ...


class AsyncWorkerClient(Protocol):
    async def amaterialize(self, spec: ImageSpec) -> Path:
        ...


class ImgCacheClient:
    def __init__(
        self,
        root: str | Path,
        worker: WorkerClient | AsyncWorkerClient | None = None,
        *,
        metadata_cache_size: int = 4096,
    ) -> None:
        self.root = Path(root)
        self.raw = OriginalsStore(self.root / "raw")
        self.layout = CacheLayout(self.root / "cache")
        self.worker = worker
        self._metadata_cache_size = max(0, metadata_cache_size)
        self._metadata_cache_lock = Lock()
        self._metadata_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @classmethod
    def zmq(
        cls,
        root: str | Path,
        endpoint: str,
        *,
        request_retries: int = 2,
        timeout_ms: int = 300_000,
        context: Any | None = None,
    ) -> Self:
        from imgcache.zmq_client import ZmqWorkerClient

        return cls(
            root,
            ZmqWorkerClient(
                endpoint,
                request_retries=request_retries,
                timeout_ms=timeout_ms,
                context=context,
            ),
        )

    def open(
        self,
        path: str | Path,
        *,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CachedImage:
        source_path = Path(path)
        source_file_id = _memoized_file_id(source_path)
        source = self.raw.put(source_path, mime=mime, metadata=metadata, source_file_id=source_file_id)
        return CachedImage(self, source)

    async def aopen(
        self,
        path: str | Path,
        *,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CachedImage:
        return await asyncio.to_thread(self.open, path, mime=mime, metadata=metadata)

    def path_for(self, spec: ImageSpec) -> Path:
        if spec.is_original:
            return self.raw.path_for(spec.source.file_id)
        if spec.operations and spec.encode is None:
            raise ValueError(TRANSFORMED_WITHOUT_ENCODE)
        if spec.encode is None:
            return self.raw.path_for(spec.source.file_id)
        return self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)

    def get(self, spec: ImageSpec) -> Path:
        path = self.path_for(spec)
        if spec.encode is None:
            if path.exists():
                return path
            raise FileNotFoundError(path)

        try:
            with open_cache_hit(path):
                return path
        except FileNotFoundError:
            if self.worker is None:
                raise
            self.worker.materialize(spec)  # type: ignore[attr-defined]
            return path

    async def aget(self, spec: ImageSpec) -> Path:
        path = self.path_for(spec)
        if spec.encode is None:
            if path.exists():
                return path
            raise FileNotFoundError(path)

        try:
            with open_cache_hit(path):
                return path
        except FileNotFoundError:
            if self.worker is None:
                raise
            if hasattr(self.worker, "amaterialize"):
                await self.worker.amaterialize(spec)  # type: ignore[attr-defined]
            else:
                await asyncio.to_thread(self.worker.materialize, spec)  # type: ignore[attr-defined]
            return path

    def identify(self, spec: ImageSpec) -> dict[str, Any]:
        cached = self._get_metadata(spec.parent_key)
        if cached is not None:
            return cached
        worker = self.worker
        if worker is None:
            raise RuntimeError("metadata requires a worker; create the client with ImgCacheClient.zmq(...)")
        if hasattr(worker, "identify"):
            meta = worker.identify(spec)  # type: ignore[attr-defined]
        else:
            meta = worker.measure(spec)  # type: ignore[attr-defined]
        self._put_metadata(spec.parent_key, meta)
        return dict(meta)

    async def aidentify(self, spec: ImageSpec) -> dict[str, Any]:
        cached = self._get_metadata(spec.parent_key)
        if cached is not None:
            return cached
        worker = self.worker
        if worker is None:
            raise RuntimeError("metadata requires a worker; create the client with ImgCacheClient.zmq(...)")
        if hasattr(worker, "aidentify"):
            meta = await worker.aidentify(spec)  # type: ignore[attr-defined]
        elif hasattr(worker, "identify"):
            meta = await asyncio.to_thread(worker.identify, spec)  # type: ignore[attr-defined]
        else:
            meta = await asyncio.to_thread(worker.measure, spec)  # type: ignore[attr-defined]
        self._put_metadata(spec.parent_key, meta)
        return dict(meta)

    def _get_metadata(self, key: str) -> dict[str, Any] | None:
        if self._metadata_cache_size == 0:
            return None
        with self._metadata_cache_lock:
            meta = self._metadata_cache.get(key)
            if meta is None:
                return None
            self._metadata_cache.move_to_end(key)
            return dict(meta)

    def _put_metadata(self, key: str, meta: dict[str, Any]) -> None:
        if self._metadata_cache_size == 0:
            return
        with self._metadata_cache_lock:
            self._metadata_cache[key] = dict(meta)
            self._metadata_cache.move_to_end(key)
            while len(self._metadata_cache) > self._metadata_cache_size:
                self._metadata_cache.popitem(last=False)


@dataclass(frozen=True)
class CachedImage:
    client: ImgCacheClient
    source: SourceSpec
    operations: tuple[Operation, ...] = ()
    encode: EncodeSpec | None = None
    # Lazily fetched libvips metadata for this exact pipeline. init=False so
    # dataclasses.replace() resets it to None on every derived CachedImage.
    _meta_cache: dict[str, Any] | None = field(default=None, init=False, compare=False, repr=False)

    @property
    def spec(self) -> ImageSpec:
        return ImageSpec(self.source, self.operations, self.encode)

    def page(self, page: int = 1, *, dpi: int | float | None = None, **params: Any) -> Self:
        payload = {"page": page, **params}
        if dpi is not None:
            payload["dpi"] = dpi
        return self._append("render", payload)

    def normalize(self, *, colorspace: str = "srgb") -> Self:
        return self._append("normalize", {"colorspace": colorspace})

    def scale(
        self,
        *,
        longest_edge: int | None = None,
        width: int | None = None,
        height: int | None = None,
        scale_factor: float | None = None,
    ) -> Self:
        return self._append(
            "scale",
            _clean_params(
                longest_edge=longest_edge,
                width=width,
                height=height,
                scale_factor=scale_factor,
            ),
        )

    def resize(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
        longest_edge: int | None = None,
        scale_factor: float | None = None,
    ) -> Self:
        return self._append(
            "resize",
            _clean_params(
                width=width,
                height=height,
                longest_edge=longest_edge,
                scale_factor=scale_factor,
            ),
        )

    def crop(self, *, x: int, y: int, w: int, h: int) -> Self:
        return self._append("crop", {"x": x, "y": y, "w": w, "h": h})

    def crop_fraction(
        self,
        *,
        left: float = 0.0,
        top: float = 0.0,
        right: float = 1.0,
        bottom: float = 1.0,
    ) -> Self:
        """Crop a normalized box (0..1) resolved against the libvips pixel size on the worker.

        Resolution-independent: e.g. ``crop_fraction(bottom=0.5)`` keeps the top half
        regardless of the source/render resolution.
        """
        return self._append(
            "crop_fraction",
            {"left": left, "top": top, "right": right, "bottom": bottom},
        )

    def rotate(self, degrees: int | float) -> Self:
        return self._append("rotate", {"degrees": degrees})

    def fast_rotate(self, degrees: int) -> Self:
        return self._append("fast_rotate", {"degrees": degrees})

    def flip(self) -> Self:
        return self._append("flip")

    def flop(self) -> Self:
        return self._append("flop")

    def webp(self, *, quality: int = 82, **params: Any) -> Self:
        return self._encode("webp", quality=quality, **params)

    def png(self, **params: Any) -> Self:
        return self._encode("png", **params)

    def jpg(self, *, quality: int = 85, **params: Any) -> Self:
        return self._encode("jpg", quality=quality, **params)

    def avif(self, *, quality: int = 60, **params: Any) -> Self:
        return self._encode("avif", quality=quality, **params)

    def tif(self, **params: Any) -> Self:
        return self._encode("tif", **params)

    @property
    def info(self) -> dict[str, Any]:
        """libvips metadata for this exact pipeline (Pillow-like ``Image.info``)."""
        return dict(self._meta())

    @property
    def size(self) -> tuple[int, int]:
        self._require_raster()
        meta = self._meta()
        return (int(meta["width"]), int(meta["height"]))

    @property
    def width(self) -> int:
        return self.size[0]

    @property
    def height(self) -> int:
        return self.size[1]

    @property
    def mode(self) -> str:
        self._require_raster()
        return str(self._meta()["mode"])

    @property
    def n_pages(self) -> int:
        return int(self._meta().get("n_pages", 1))

    def identify(self) -> dict[str, Any]:
        """Force a metadata fetch and return the full libvips metadata dict."""
        return dict(self._meta())

    async def ainfo(self) -> dict[str, Any]:
        if self._meta_cache is None:
            object.__setattr__(self, "_meta_cache", await self.client.aidentify(self.spec))
        return dict(self._meta_cache)

    def _meta(self) -> dict[str, Any]:
        if self._meta_cache is None:
            object.__setattr__(self, "_meta_cache", self.client.identify(self.spec))
        return self._meta_cache

    def _is_paged(self) -> bool:
        return any(operation.name == "render" for operation in self.operations)

    def _require_raster(self) -> None:
        if self.source.is_pdf() and not self._is_paged():
            raise ValueError("select a PDF page first with .page(...) before reading size or mode")

    def path(self) -> Path:
        return self.client.get(self.spec)

    async def apath(self) -> Path:
        return await self.client.aget(self.spec)

    def open(self, mode: str = "rb"):
        return open_cache_hit(self.path(), mode)

    def bytes(self) -> bytes:
        return self.path().read_bytes()

    async def abytes(self) -> bytes:
        path = await self.apath()
        return await asyncio.to_thread(path.read_bytes)

    def __fspath__(self) -> str:
        return str(self.path())

    def __await__(self):
        return self.apath().__await__()

    def _append(self, name: str, params: dict[str, Any] | None = None) -> Self:
        return replace(
            self,
            operations=self.operations + (Operation(name, params or {}),),
            encode=None,
        )

    def _encode(self, output_format: str, **params: Any) -> Self:
        spec = ImageSpec(self.source, self.operations)
        encoded = spec.with_encode(Operation("encode", {"format": output_format, **params}))
        return replace(self, encode=encoded.encode)


def _clean_params(**params: Any) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _memoized_file_id(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    key = (resolved, stat.st_mtime_ns, stat.st_size)
    with _INGEST_MEMO_LOCK:
        cached = _INGEST_MEMO.get(key)
        if cached is not None:
            _INGEST_MEMO.move_to_end(key)
            return cached

    digest = file_id(resolved)
    with _INGEST_MEMO_LOCK:
        _INGEST_MEMO[key] = digest
        _INGEST_MEMO.move_to_end(key)
        while len(_INGEST_MEMO) > _INGEST_MEMO_MAX:
            _INGEST_MEMO.popitem(last=False)
    return digest
