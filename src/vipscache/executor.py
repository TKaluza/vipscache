from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from vipscache.spec import EncodeSpec, NodeSpec, SourceSpec


class ImageExecutor(Protocol):
    def load_source(self, path: Path) -> Any:
        ...

    def render_source(self, source: SourceSpec, path: Path, spec: NodeSpec) -> Any:
        ...

    def load_node(self, path: Path) -> Any:
        ...

    def apply_node(self, image: Any, spec: NodeSpec) -> Any:
        ...

    def write_node(self, image: Any, path: Path) -> None:
        ...

    def write_leaf(self, image: Any, spec: EncodeSpec, path: Path) -> None:
        ...

    def metadata(self, image: Any) -> dict[str, Any]:
        ...


class VipsExecutor:
    """Worker-side executor backed by pyvips/libvips."""

    def __init__(self) -> None:
        try:
            import pyvips
        except ImportError as error:
            raise RuntimeError("RenderWorker requires pyvips; install vipscache[worker].") from error
        self._pyvips = pyvips

    def libvips_version(self) -> str:
        return ".".join(str(self._pyvips.version(part)) for part in range(3))

    def load_source(self, path: Path) -> Any:
        return self._pyvips.Image.new_from_file(str(path), access="random")

    def render_source(self, source: SourceSpec, path: Path, spec: NodeSpec) -> Any:
        if self._is_pdf(source):
            return self._render_pdf(path, spec.operation.params)
        return self.apply_node(self.load_source(path), spec)

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
            return self._crop(
                image,
                int(params["x"]),
                int(params["y"]),
                int(params["w"]),
                int(params["h"]),
            )
        if op.name == "crop_fraction":
            return self._crop_fraction(
                image,
                float(params.get("left", 0.0)),
                float(params.get("top", 0.0)),
                float(params.get("right", 1.0)),
                float(params.get("bottom", 1.0)),
            )
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

    def metadata(self, image: Any) -> dict[str, Any]:
        fields = set(image.get_fields())
        meta: dict[str, Any] = {
            "width": int(image.width),
            "height": int(image.height),
            "bands": int(image.bands),
            "interpretation": str(image.interpretation),
            "band_format": str(image.format),
            "has_alpha": bool(image.hasalpha()),
            "mode": _interpretation_to_mode(image),
            "xres": float(image.xres),
            "yres": float(image.yres),
            "dpi": [round(image.xres * 25.4, 2), round(image.yres * 25.4, 2)],
        }
        if "n-pages" in fields:
            meta["n_pages"] = int(image.get("n-pages"))
        if "page-height" in fields:
            meta["page_height"] = int(image.get("page-height"))
        if "orientation" in fields:
            meta["orientation"] = int(image.get("orientation"))
        if "vips-loader" in fields:
            loader = str(image.get("vips-loader"))
            meta["loader"] = loader
            meta["format"] = _format_from_loader(loader)
        return meta

    def _crop(self, image: Any, x: int, y: int, w: int, h: int) -> Any:
        if w <= 0 or h <= 0:
            raise ValueError(f"crop width and height must be positive (w={w}, h={h})")
        if x < 0 or y < 0 or x + w > image.width or y + h > image.height:
            raise ValueError(
                f"crop region (x={x}, y={y}, w={w}, h={h}) is outside image bounds "
                f"({image.width}x{image.height})"
            )
        return image.crop(x, y, w, h)

    def _crop_fraction(self, image: Any, left: float, top: float, right: float, bottom: float) -> Any:
        for name, value in (("left", left), ("top", top), ("right", right), ("bottom", bottom)):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"crop_fraction {name} must be within [0.0, 1.0], got {value}")
        if right <= left or bottom <= top:
            raise ValueError(
                f"crop_fraction requires left<right and top<bottom "
                f"(left={left}, right={right}, top={top}, bottom={bottom})"
            )
        width = image.width
        height = image.height
        x = int(round(left * width))
        y = int(round(top * height))
        w = max(1, min(int(round(right * width)) - x, width - x))
        h = max(1, min(int(round(bottom * height)) - y, height - y))
        return image.crop(x, y, w, h)

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

    def _render_pdf(self, path: Path, params: dict[str, object]) -> Any:
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

        rendered = self._pyvips.Image.pdfload(str(path), **options)
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
        return source.is_pdf()


def _interpretation_to_mode(image: Any) -> str:
    """Map a libvips interpretation/bands to a Pillow-style mode string."""
    interpretation = str(image.interpretation).lower()
    alpha = bool(image.hasalpha())
    if interpretation in {"srgb", "rgb"}:
        return "RGBA" if alpha else "RGB"
    if interpretation in {"b-w", "grey16"}:
        return "LA" if alpha else "L"
    if interpretation == "cmyk":
        return "CMYK"
    return interpretation.upper()


def _format_from_loader(loader: str) -> str:
    """Map a libvips loader name (e.g. 'jpegload') to a Pillow-style format ('JPEG')."""
    name = loader.split("_", 1)[0]
    if name.endswith("load"):
        name = name[: -len("load")]
    aliases = {"jpg": "JPEG", "jpeg": "JPEG", "tif": "TIFF", "tiff": "TIFF"}
    return aliases.get(name, name.upper())
