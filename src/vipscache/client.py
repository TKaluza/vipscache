from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import IO, Any, Protocol, Self

from vipscache.errors import ErrorType, VipsCacheError
from vipscache.hash import file_id
from vipscache.io import open_cache_hit
from vipscache.layout import CacheLayout
from vipscache.originals import OriginalsStore
from vipscache.spec import (
    EncodeSpec,
    ImageSpec,
    MaterializePolicy,
    Operation,
    RetainPolicy,
    SourceSpec,
)

TRANSFORMED_WITHOUT_ENCODE = (
    "transformed CachedImage must choose an output format; call .webp(), .png(), or .jpg()"
)
_INGEST_MEMO_MAX = 4096
_INGEST_MEMO_LOCK = Lock()
_INGEST_MEMO: OrderedDict[tuple[Path, int, int], str] = OrderedDict()


class WorkerClient(Protocol):
    def materialize(self, spec: ImageSpec) -> Path: ...


class AsyncWorkerClient(Protocol):
    async def amaterialize(self, spec: ImageSpec) -> Path: ...


class MetadataWorkerClient(Protocol):
    """A worker that provides metadata without requiring a renderer."""

    def identify(self, spec: ImageSpec) -> dict[str, Any]:
        """Fetch pipeline metadata, reusing the bounded local metadata cache."""
        ...


class VipsCacheClient:
    """Read shared cache files directly and delegate misses to an optional worker."""

    def __init__(
        self,
        root: str | Path,
        worker: WorkerClient | AsyncWorkerClient | MetadataWorkerClient | None = None,
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
        """Create a client backed by the optional ZeroMQ worker transport."""
        from vipscache.zmq_client import ZmqWorkerClient

        return cls(
            root,
            ZmqWorkerClient(
                endpoint,
                request_retries=request_retries,
                timeout_ms=timeout_ms,
                context=context,
            ),
        )

    def register(
        self,
        path: str | Path,
        *,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
        file_id: str | None = None,
        verify: bool = False,
    ) -> CachedImage:
        """Publish an original copy, reusing a memoized content hash when possible."""
        source_path = Path(path)
        source_file_id = file_id if file_id is not None else _memoized_file_id(source_path)
        source = self.raw.put(
            source_path, mime=mime, metadata=metadata, source_file_id=source_file_id, verify=verify
        )
        return CachedImage(self, source)

    async def aregister(
        self,
        path: str | Path,
        *,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
        file_id: str | None = None,
        verify: bool = False,
    ) -> CachedImage:
        return await asyncio.to_thread(
            self.register, path, mime=mime, metadata=metadata, file_id=file_id, verify=verify
        )

    def link(
        self,
        path: str | Path,
        *,
        file_id: str | None = None,
        mode: str = "hardlink",
        link_target: str | Path | None = None,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
        verify: bool = False,
    ) -> CachedImage:
        """Publish a hardlink or symlink to the original without copying its content."""
        return CachedImage(
            self,
            self.raw.link(
                path,
                file_id=file_id,
                mode=mode,
                link_target=link_target,
                mime=mime,
                metadata=metadata,
                verify=verify,
            ),
        )

    async def alink(
        self,
        path: str | Path,
        *,
        file_id: str | None = None,
        mode: str = "hardlink",
        link_target: str | Path | None = None,
        mime: str | None = None,
        metadata: dict[str, Any] | None = None,
        verify: bool = False,
    ) -> CachedImage:
        return await asyncio.to_thread(
            self.link,
            path,
            file_id=file_id,
            mode=mode,
            link_target=link_target,
            mime=mime,
            metadata=metadata,
            verify=verify,
        )

    def unlink(self, file_id: str) -> None:
        self.raw.unlink(file_id)

    async def aunlink(self, file_id: str) -> None:
        await asyncio.to_thread(self.unlink, file_id)

    def cleanup_staging(self, *, older_than: float = 24 * 60 * 60) -> int:
        return self.raw.cleanup_staging(older_than=older_than)

    async def acleanup_staging(self, *, older_than: float = 24 * 60 * 60) -> int:
        return await asyncio.to_thread(self.cleanup_staging, older_than=older_than)

    @staticmethod
    def _spec(image: ImageSpec | CachedImage) -> ImageSpec:
        return image.spec if isinstance(image, CachedImage) else image

    def ensure(
        self, image: ImageSpec | CachedImage, *, retain: RetainPolicy | str = RetainPolicy.TTL
    ) -> Path:
        """Guarantee requested intermediates and explicitly apply leaf retention."""
        spec = self._spec(image)
        policy = RetainPolicy(retain)
        self._worker_method("ensure")(spec, retain=policy)
        return self.resolve(spec)

    async def aensure(
        self, image: ImageSpec | CachedImage, *, retain: RetainPolicy | str = RetainPolicy.TTL
    ) -> Path:
        """Ensure intermediates and retention using async transport or a worker thread."""
        spec = self._spec(image)
        policy = RetainPolicy(retain)
        if method := self._optional_worker_method("aensure"):
            await method(spec, retain=policy)
        else:
            await asyncio.to_thread(self._worker_method("ensure"), spec, retain=policy)
        return self.resolve(spec)

    def unpin(self, image: ImageSpec | CachedImage) -> None:
        self._worker_method("unpin")(self._spec(image))

    async def aunpin(self, image: ImageSpec | CachedImage) -> None:
        if method := self._optional_worker_method("aunpin"):
            await method(self._spec(image))
        else:
            await asyncio.to_thread(self.unpin, image)

    def delete(self, images: list[ImageSpec | CachedImage]) -> None:
        self._worker_method("delete")([self._spec(image) for image in images])

    async def adelete(self, images: list[ImageSpec | CachedImage]) -> None:
        if method := self._optional_worker_method("adelete"):
            await method([self._spec(image) for image in images])
        else:
            await asyncio.to_thread(self.delete, images)

    def _optional_worker_method(self, name: str) -> Callable[..., Any] | None:
        """Discover optional worker capabilities at the dynamic adapter boundary."""
        method = getattr(self.worker, name, None)
        return method if callable(method) else None

    def _worker_method(self, name: str) -> Callable[..., Any]:
        """Require one capability without rejecting older or partial worker adapters."""
        method = self._optional_worker_method(name)
        if method is None:
            raise VipsCacheError(
                ErrorType.UNSUPPORTED_CAPABILITY, f"{name} requires a compatible worker"
            )
        return method

    def resolve(self, spec: ImageSpec) -> Path:
        """Calculate the local cache path without checking files or rendering."""
        if spec.is_original:
            return self.raw.path_for(spec.source.file_id)
        if spec.operations and spec.encode is None:
            raise ValueError(TRANSFORMED_WITHOUT_ENCODE)
        if spec.encode is None:
            return self.raw.path_for(spec.source.file_id)
        return self.layout.leaf_path(spec.leaf.key, spec.leaf.extension)

    async def aresolve(self, spec: ImageSpec) -> Path:
        """Async counterpart of resolve(); performs no I/O."""
        return self.resolve(spec)

    def get(self, spec: ImageSpec) -> Path:
        """Return a readable local path, rendering a missing leaf when possible."""
        path = self.resolve(spec)
        with self._open(spec):
            return path

    async def aget(self, spec: ImageSpec) -> Path:
        """Return a readable local path without blocking the event loop on rendering."""
        path = self.resolve(spec)
        with await self._aopen(spec):
            return path

    def _open(self, spec: ImageSpec, mode: str = "rb") -> IO[Any]:
        """Open the hit itself, retrying at most twice after a miss or cleanup race."""
        path = self.resolve(spec)
        for _ in range(2):
            try:
                return open_cache_hit(path, mode)
            except FileNotFoundError:
                if spec.encode is None or self.worker is None:
                    raise
                self._worker_method("materialize")(spec)
        return open_cache_hit(path, mode)

    async def _aopen(self, spec: ImageSpec, mode: str = "rb") -> IO[Any]:
        """Open a hit asynchronously, retaining the same bounded miss policy as _open."""
        path = self.resolve(spec)
        for _ in range(2):
            try:
                return await asyncio.to_thread(open_cache_hit, path, mode)
            except FileNotFoundError:
                if spec.encode is None or self.worker is None:
                    raise
                if method := self._optional_worker_method("amaterialize"):
                    await method(spec)
                else:
                    await asyncio.to_thread(self._worker_method("materialize"), spec)
        return await asyncio.to_thread(open_cache_hit, path, mode)

    def identify(self, spec: ImageSpec) -> dict[str, Any]:
        """Fetch pipeline metadata, reusing the bounded local metadata cache."""
        cached = self._get_metadata(spec.parent_key)
        if cached is not None:
            return cached
        worker = self.worker
        if worker is None:
            raise RuntimeError(
                "metadata requires a worker; create the client with VipsCacheClient.zmq(...)"
            )
        method = self._optional_worker_method("identify") or self._worker_method("measure")
        meta = method(spec)
        self._put_metadata(spec.parent_key, meta)
        return dict(meta)

    async def aidentify(self, spec: ImageSpec) -> dict[str, Any]:
        """Fetch pipeline metadata without blocking the event loop on worker I/O."""
        cached = self._get_metadata(spec.parent_key)
        if cached is not None:
            return cached
        worker = self.worker
        if worker is None:
            raise RuntimeError(
                "metadata requires a worker; create the client with VipsCacheClient.zmq(...)"
            )
        if method := self._optional_worker_method("aidentify"):
            meta = await method(spec)
        else:
            method = self._optional_worker_method("identify") or self._worker_method("measure")
            meta = await asyncio.to_thread(method, spec)
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
    """Immutable image pipeline with direct reads and explicit retention commands."""

    client: VipsCacheClient
    source: SourceSpec
    operations: tuple[Operation, ...] = ()
    encode: EncodeSpec | None = None
    # Lazily fetched libvips metadata for this exact pipeline. init=False so
    # dataclasses.replace() resets it to None on every derived CachedImage.
    _meta_cache: dict[str, Any] | None = field(default=None, init=False, compare=False, repr=False)

    @property
    def spec(self) -> ImageSpec:
        return ImageSpec(self.source, self.operations, self.encode)

    def materialize(self, policy: str | MaterializePolicy) -> Self:
        if not self.operations:
            raise ValueError("materialize requires a preceding operation")
        operations = self.operations[:-1] + (
            replace(self.operations[-1], materialize=MaterializePolicy(policy)),
        )
        return replace(self, operations=operations)

    def autorotate(self) -> Self:
        return self._append("autorotate")

    def thumbnail(
        self,
        *,
        longest_edge: int | None = None,
        width: int | None = None,
        height: int | None = None,
        autorotate: bool = True,
    ) -> Self:
        return self._append(
            "thumbnail",
            _clean_params(
                longest_edge=longest_edge, width=width, height=height, autorotate=autorotate
            ),
        )

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

    async def aidentify(self) -> dict[str, Any]:
        """Fetch pipeline metadata without blocking the event loop on worker I/O."""
        meta = self._meta_cache
        if meta is None:
            meta = await self.client.aidentify(self.spec)
            object.__setattr__(self, "_meta_cache", meta)
        return dict(meta)

    def _meta(self) -> dict[str, Any]:
        meta = self._meta_cache
        if meta is None:
            meta = self.client.identify(self.spec)
            object.__setattr__(self, "_meta_cache", meta)
        return meta

    def _is_paged(self) -> bool:
        return any(operation.name == "render" for operation in self.operations)

    def _require_raster(self) -> None:
        if self.source.is_pdf() and not self._is_paged():
            raise ValueError("select a PDF page first with .page(...) before reading size or mode")

    def path(self) -> Path:
        return self.client.get(self.spec)

    async def apath(self) -> Path:
        return await self.client.aget(self.spec)

    def ensure(self, *, retain: RetainPolicy | str = RetainPolicy.TTL) -> Path:
        """Guarantee requested intermediates and explicitly apply leaf retention."""
        return self.client.ensure(self, retain=retain)

    async def aensure(self, *, retain: RetainPolicy | str = RetainPolicy.TTL) -> Path:
        """Ensure intermediates and retention using async transport or a worker thread."""
        return await self.client.aensure(self, retain=retain)

    def unpin(self) -> None:
        self.client.unpin(self)

    async def aunpin(self) -> None:
        await self.client.aunpin(self)

    def delete(self) -> None:
        self.client.delete([self])

    async def adelete(self) -> None:
        await self.client.adelete([self])

    def open(self, mode: str = "rb") -> IO[Any]:
        """Open a cache hit, rendering a missing leaf when possible."""
        return self.client._open(self.spec, mode)

    async def aopen(self, mode: str = "rb") -> IO[Any]:
        """Open a cache hit without blocking the event loop on file I/O."""
        return await self.client._aopen(self.spec, mode)

    def read_bytes(self) -> bytes:
        with self.open() as handle:
            return handle.read()

    async def aread_bytes(self) -> bytes:
        with await self.aopen() as handle:
            return await asyncio.to_thread(handle.read)

    def __fspath__(self) -> str:
        return str(self.path())

    def __await__(self):
        return self.apath().__await__()

    def _append(self, name: str, params: dict[str, Any] | None = None) -> Self:
        spec = ImageSpec(self.source, self.operations + (Operation(name, params or {}),))
        return replace(self, operations=spec.operations, encode=None)

    def _encode(self, output_format: str, **params: Any) -> Self:
        if "retain" in params:
            raise ValueError("retain belongs to ensure(), not the encoder")
        spec = ImageSpec(self.source, self.operations)
        encoded = spec.with_encode(Operation("encode", {"format": output_format, **params}))
        return replace(self, encode=encoded.encode)


def _clean_params(**params: Any) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _memoized_file_id(path: Path) -> str:
    """Cache content hashes by resolved path, modification time and size."""
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
