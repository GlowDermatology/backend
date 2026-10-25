"""Upload image processing (admin API v1, section 2.3 product images).

Contract: "≤5 MiB (`file_too_large`); must decode in Pillow as JPEG, PNG or WebP (`unsupported_image`)".
The image is detected by content (never by file name or Content-Type), EXIF-transposed, downscaled so
that its longest side is at most ``max_side`` and re-encoded in the same format without EXIF, ICC or
XMP metadata.
"""

import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal

from PIL import Image, ImageOps

type ImageFormat = Literal["jpeg", "png", "webp"]

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 2400
# Pixel budget before decoding: a 5 MiB PNG can expand to gigabytes in memory (decompression bomb).
MAX_PIXELS = 40_000_000
FORMATS: dict[str, tuple[ImageFormat, str, str]] = {
    "JPEG": ("jpeg", "image/jpeg", "jpg"),
    "PNG": ("png", "image/png", "png"),
    "WEBP": ("webp", "image/webp", "webp"),
}


class ImageError(Exception):
    """``code`` is the contract field code: ``file_too_large`` or ``unsupported_image``."""

    def __init__(self, code: Literal["file_too_large", "unsupported_image"]) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    content: bytes
    format: ImageFormat
    content_type: str
    extension: str
    width: int
    height: int


def _read(uploaded: Any, max_bytes: int) -> bytes:
    if isinstance(uploaded, (bytes, bytearray, memoryview)):
        content = bytes(uploaded)
    else:
        size = getattr(uploaded, "size", None)
        if isinstance(size, int) and size > max_bytes:
            raise ImageError("file_too_large")
        if hasattr(uploaded, "seek"):
            uploaded.seek(0)
        content = uploaded.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ImageError("file_too_large")
    return content


def _prepare_mode(image: Image.Image, fmt: ImageFormat) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    if fmt == "jpeg":
        return image if image.mode in ("RGB", "L") else image.convert("RGB")
    if fmt == "png":
        if image.mode in ("RGB", "RGBA", "L", "LA") and "transparency" not in image.info:
            return image
        if image.mode == "1" or (image.mode.startswith("I") and not has_alpha):
            return image.convert("L")
        return image.convert("RGBA" if has_alpha else "RGB")
    if image.mode in ("RGB", "RGBA"):
        return image
    return image.convert("RGBA" if has_alpha else "RGB")


def process_image(uploaded: Any, *, max_bytes: int = MAX_BYTES, max_side: int = MAX_SIDE) -> ProcessedImage:
    """Validate and normalise an uploaded image (a Django ``UploadedFile``, a binary stream or ``bytes``)."""
    content = _read(uploaded, max_bytes)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                spec = FORMATS.get(source.format or "")
                if spec is None:
                    raise ImageError("unsupported_image")
                if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1:
                    raise ImageError("unsupported_image")
                if source.width * source.height > MAX_PIXELS:
                    raise ImageError("unsupported_image")
                source.load()
                image = ImageOps.exif_transpose(source) or source.copy()
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError, TypeError, Image.DecompressionBombError, Warning) as exc:
        raise ImageError("unsupported_image") from exc
    fmt, content_type, extension = spec
    image = _prepare_mode(image, fmt)
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    image.info.clear()
    output = BytesIO()
    if fmt == "jpeg":
        image.save(output, format="JPEG", quality=88, progressive=True, optimize=True)
    elif fmt == "png":
        image.save(output, format="PNG", optimize=True)
    else:
        image.save(output, format="WEBP", quality=88, method=6)
    return ProcessedImage(
        content=output.getvalue(),
        format=fmt,
        content_type=content_type,
        extension=extension,
        width=image.width,
        height=image.height,
    )
