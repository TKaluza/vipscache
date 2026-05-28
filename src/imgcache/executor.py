from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from imgcache.spec import EncodeSpec, NodeSpec, SourceSpec


class ImageExecutor(Protocol):
    def load_source(self, source: SourceSpec) -> Any:
        ...

    def render_source(self, source: SourceSpec, spec: NodeSpec) -> Any:
        ...

    def load_node(self, path: Path) -> Any:
        ...

    def apply_node(self, image: Any, spec: NodeSpec) -> Any:
        ...

    def write_node(self, image: Any, path: Path) -> None:
        ...

    def write_leaf(self, image: Any, spec: EncodeSpec, path: Path) -> None:
        ...


class VipsExecutor:
    """Worker-side executor backed by pyvips/libvips."""

    def __init__(self) -> None:
        try:
            import pyvips
        except ImportError as error:
            raise RuntimeError("RenderWorker requires pyvips; install imgcache[worker].") from error
        self._pyvips = pyvips

    def load_source(self, source: SourceSpec) -> Any:
        return self._pyvips.Image.new_from_file(source.original_path, access="random")

    def render_source(self, source: SourceSpec, spec: NodeSpec) -> Any:
        if self._is_pdf(source):
            return self._render_pdf(source, spec.operation.params)
        return self.apply_node(self.load_source(source), spec)

    def load_node(self, path: Path) -> Any:
        with path.open("rb"):
            pass
        return self._pyvips.Image.new_from_file(str(path), access="random")

    def apply_node(self, image: Any, spec: NodeSpec) -> Any:
        op = spec.operation
        params = op.params

        if op.name == "render":
            return self._render(image, params)
        if op.name in {"normalize", "colorspace"}:
            return self._colorspace(image, str(params.get("colorspace", "srgb")))
        if op.name == "fast_rotate":
            degrees = int(params.get("degrees", params.get("angle", 0))) % 360
            if degrees not in {0, 90, 180, 270}:
                raise ValueError("fast_rotate only supports 0, 90, 180, or 270 degrees")
            return self._fast_rotate(image, degrees)
        if op.name == "rotate":
            degrees = float(params["degrees"])
            return image.similarity(angle=degrees, interpolate=self._pyvips.Interpolate.new("bicubic"))
        if op.name == "flip":
            return image.flip("vertical")
        if op.name == "flop":
            return image.flip("horizontal")
        if op.name == "crop":
            x = int(params["x"])
            y = int(params["y"])
            w = int(params["w"])
            h = int(params["h"])
            return image.crop(x, y, w, h)
        if op.name in {"scale", "resize"}:
            width = params.get("width")
            height = params.get("height")
            longest_edge = params.get("longest_edge")
            scale_factor = params.get("scale_factor")
            return self._resize(image, width, height, longest_edge, scale_factor)

        raise ValueError(f"unsupported operation: {op.name}")

    def write_node(self, image: Any, path: Path) -> None:
        image.write_to_file(str(path))

    def write_leaf(self, image: Any, spec: EncodeSpec, path: Path) -> None:
        options = dict(spec.params)
        output = image
        if spec.format in {"jpg", "jpeg", "webp"}:
            output = self._flatten_if_alpha(output, options.pop("background", [255, 255, 255]))
        if "quality" in options:
            options["Q"] = int(options.pop("quality"))
        output.write_to_file(str(path), **options)

    def _render(self, image: Any, params: dict[str, object]) -> Any:
        if "dpi" in params:
            raise ValueError("dpi render strategy is only supported for PDF sources")
        rendered = self._colorspace(image, str(params.get("colorspace", "srgb")))

        width = params.get("width")
        height = params.get("height")
        longest_edge = params.get("longest_edge")
        scale_factor = params.get("scale_factor")
        resize_args = [width, height, longest_edge, scale_factor]
        if sum(value is not None for value in resize_args) > 1:
            raise ValueError("render accepts exactly one size strategy")
        if any(value is not None for value in resize_args):
            return self._resize(rendered, width, height, longest_edge, scale_factor)
        return rendered

    def _render_pdf(self, source: SourceSpec, params: dict[str, object]) -> Any:
        options: dict[str, object] = {"access": "random"}
        page = int(params.get("page", 1))
        if page < 1:
            raise ValueError("render page is one-based and must be >= 1")
        options["page"] = page - 1
        options["n"] = int(params.get("n", 1))
        width = params.get("width")
        height = params.get("height")
        longest_edge = params.get("longest_edge")
        size_strategies = [
            "dpi" in params,
            "scale_factor" in params,
            width is not None or height is not None,
            longest_edge is not None,
        ]
        if sum(size_strategies) > 1:
            raise ValueError("render accepts exactly one size strategy")

        if "dpi" in params:
            options["dpi"] = float(params["dpi"])
        if "scale_factor" in params:
            options["scale"] = float(params["scale_factor"])
        if "background" in params:
            options["background"] = params["background"]

        rendered = self._pyvips.Image.pdfload(source.original_path, **options)
        rendered = self._colorspace(rendered, str(params.get("colorspace", "srgb")))
        if width is not None or height is not None or longest_edge is not None:
            return self._resize(rendered, width, height, longest_edge, None)
        return rendered

    def _resize(
        self,
        image: Any,
        width: object | None,
        height: object | None,
        longest_edge: object | None,
        scale_factor: object | None,
    ) -> Any:
        strategies = [width is not None or height is not None, longest_edge is not None, scale_factor is not None]
        if sum(strategies) != 1:
            raise ValueError("resize accepts exactly one size strategy")

        if scale_factor is not None:
            factor = float(scale_factor)
        elif longest_edge is not None:
            edge = int(longest_edge)
            factor = edge / max(image.width, image.height)
        else:
            if width is None:
                ratio = int(height) / image.height
                factor = ratio
            elif height is None:
                ratio = int(width) / image.width
                factor = ratio
            else:
                return image.resize(int(width) / image.width, vscale=int(height) / image.height)

        if factor <= 0:
            raise ValueError("resize dimensions must be positive")
        return image.resize(factor)

    def _colorspace(self, image: Any, colorspace: str) -> Any:
        colorspace = colorspace.lower()
        if colorspace in {"srgb", "rgb"}:
            return image.colourspace("srgb")
        if colorspace in {"b-w", "bw", "grey", "gray", "greyscale", "grayscale"}:
            return image.colourspace("b-w")
        raise ValueError(f"unsupported colorspace: {colorspace}")

    def _fast_rotate(self, image: Any, degrees: int) -> Any:
        if degrees == 0:
            return image
        if degrees == 90:
            return image.rot("d90")
        if degrees == 180:
            return image.rot("d180")
        return image.rot("d270")

    def _flatten_if_alpha(self, image: Any, background: object) -> Any:
        if image.hasalpha():
            return image.flatten(background=background)
        return image

    def _is_pdf(self, source: SourceSpec) -> bool:
        if source.mime == "application/pdf":
            return True
        return source.original_path.lower().endswith(".pdf")
