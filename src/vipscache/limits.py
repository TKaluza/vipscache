from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from vipscache.spec import EncodeSpec, NodeSpec


class SizedImage(Protocol):
    width: int
    height: int


@dataclass(frozen=True)
class WorkerLimits:
    allowed_formats: frozenset[str] = field(
        default_factory=lambda: frozenset({"webp", "png", "jpg", "jpeg", "avif", "tif", "tiff"})
    )
    max_dpi: int | None = None
    max_input_pixels: int | None = None
    max_output_pixels: int | None = None
    max_copy_memory_bytes: int | None = None

    def check_leaf(self, spec: EncodeSpec) -> None:
        if spec.format not in self.allowed_formats:
            raise ValueError(f"format is not allowed: {spec.format}")

    def check_node(self, spec: NodeSpec) -> None:
        if spec.operation.name != "render" or self.max_dpi is None:
            return
        dpi = spec.operation.params.get("dpi")
        if dpi is not None and int(dpi) > self.max_dpi:
            raise ValueError(f"dpi exceeds worker limit: {dpi}")

    def check_input_image(self, image: SizedImage) -> None:
        if self.max_input_pixels is None:
            return
        self._check_pixels(image, self.max_input_pixels, "input")

    def check_output_image(self, image: SizedImage) -> None:
        if self.max_output_pixels is None:
            return
        self._check_pixels(image, self.max_output_pixels, "output")

    def _check_pixels(self, image: SizedImage, limit: int, label: str) -> None:
        pixels = image.width * image.height
        if pixels > limit:
            raise ValueError(f"{label} image exceeds pixel limit: {pixels} > {limit}")
