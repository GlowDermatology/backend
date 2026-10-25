"""Shared helpers of the catalog API tests."""

from io import BytesIO
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from glowderma.catalog.tests.factories import image_bytes


def errors_of(response: Any) -> dict[str | None, tuple[str, str]]:
    """``{field: (code, message)}`` of the first error per field (what the FE mapper keeps)."""
    body = response.json()
    assert body["code"] == "validation_error", body
    assert body["message"] == "Maydonlarni tekshiring."
    assert body["request_id"]
    result: dict[str | None, tuple[str, str]] = {}
    for item in body["errors"]:
        result.setdefault(item["field"], (item["code"], item["message"]))
    return result


def upload(
    fmt: str = "PNG",
    *,
    name: str | None = None,
    content_type: str | None = None,
    size: tuple[int, int] = (24, 16),
    content: bytes | None = None,
) -> SimpleUploadedFile:
    """A real image part (``PNG`` | ``JPEG`` | ``WEBP``) unless ``content`` is given verbatim."""
    extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[fmt]
    return SimpleUploadedFile(
        name or f"photo.{extension}",
        content if content is not None else image_bytes(fmt, size=size),
        content_type=content_type or f"image/{extension if extension != 'jpg' else 'jpeg'}",
    )


def jpeg_with_exif(size: tuple[int, int] = (60, 40)) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90°
    exif[0x010F] = "Glow Derma camera"
    output = BytesIO()
    Image.new("RGB", size, "#d6b2c4").save(output, format="JPEG", exif=exif.tobytes())
    return output.getvalue()
