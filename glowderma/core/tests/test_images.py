from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from glowderma.core.images import MAX_BYTES, ImageError, ProcessedImage, process_image


def encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    stream = BytesIO()
    image.save(stream, format=fmt, **options)
    return stream.getvalue()


def gradient(width: int, height: int, mode: str = "RGB") -> Image.Image:
    image = Image.new(mode, (width, height))
    image.putdata(
        [
            (x % 256, y % 256, (x + y) % 256, 128)[: len(image.getbands())]
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def exif_bytes(orientation: int) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Glow Derma camera"
    return exif.tobytes()


def decode(result: ProcessedImage) -> Image.Image:
    image = Image.open(BytesIO(result.content))
    image.load()
    return image


def test_jpeg_with_exif_orientation_is_transposed_and_stripped() -> None:
    """Contract 2.3: "must decode in Pillow as JPEG, PNG or WebP"; EXIF transposed, no EXIF/ICC/XMP kept."""
    source = gradient(60, 40)
    uploaded = SimpleUploadedFile(
        "photo.png",
        encode(source, "JPEG", exif=exif_bytes(6), icc_profile=b"\x00" * 128),
        content_type="image/png",
    )
    result = process_image(uploaded)
    assert (result.format, result.content_type, result.extension) == ("jpeg", "image/jpeg", "jpg")
    assert (result.width, result.height) == (40, 60)
    decoded = decode(result)
    assert decoded.format == "JPEG" and decoded.size == (40, 60)
    assert dict(decoded.getexif()) == {}
    assert "icc_profile" not in decoded.info and "xmp" not in decoded.info
    assert decoded.info.get("progressive") or decoded.info.get("progression")


def test_png_with_alpha_keeps_transparency() -> None:
    source = gradient(30, 20, "RGBA")
    result = process_image(encode(source, "PNG"))
    assert result.format == "png" and result.extension == "png" and result.content_type == "image/png"
    decoded = decode(result)
    assert decoded.format == "PNG" and decoded.mode == "RGBA"
    assert decoded.getpixel((5, 5)) == source.getpixel((5, 5))


def test_webp_round_trip() -> None:
    result = process_image(BytesIO(encode(gradient(32, 24), "WEBP", lossless=True)))
    assert result.format == "webp" and result.extension == "webp" and result.content_type == "image/webp"
    assert decode(result).format == "WEBP"
    assert (result.width, result.height) == (32, 24)


def test_oversize_image_is_downscaled_to_max_side() -> None:
    source = Image.new("RGB", (3000, 1500), "white")
    result = process_image(encode(source, "JPEG"), max_side=2400)
    assert (result.width, result.height) == (2400, 1200)
    assert decode(result).size == (2400, 1200)
    small = process_image(encode(Image.new("RGB", (100, 50), "white"), "PNG"))
    assert (small.width, small.height) == (100, 50)


def test_text_file_named_photo_jpg_is_unsupported() -> None:
    """Contract 2.3: `unsupported_image` is decided by content, never by name or Content-Type."""
    uploaded = SimpleUploadedFile("photo.jpg", b"definitely not an image", content_type="image/jpeg")
    with pytest.raises(ImageError) as caught:
        process_image(uploaded)
    assert caught.value.code == "unsupported_image"


def test_gif_is_unsupported() -> None:
    with pytest.raises(ImageError) as caught:
        process_image(encode(Image.new("P", (10, 10)), "GIF"))
    assert caught.value.code == "unsupported_image"


def test_animated_webp_is_unsupported() -> None:
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    stream = BytesIO()
    frames[0].save(stream, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ImageError) as caught:
        process_image(stream.getvalue())
    assert caught.value.code == "unsupported_image"


def test_too_large_file_is_rejected_before_decoding() -> None:
    """Contract 2.3: ≤5 MiB (`file_too_large`)."""
    content = encode(Image.new("RGB", (10, 10)), "PNG")
    with pytest.raises(ImageError) as caught:
        process_image(SimpleUploadedFile("a.png", content + b"\x00" * MAX_BYTES))
    assert caught.value.code == "file_too_large"
    with pytest.raises(ImageError) as by_bytes:
        process_image(content, max_bytes=len(content) - 1)
    assert by_bytes.value.code == "file_too_large"
    assert process_image(content, max_bytes=len(content)).format == "png"


def test_decompression_bomb_is_unsupported() -> None:
    bomb = Image.new("1", (20_000, 20_000))
    with pytest.raises(ImageError) as caught:
        process_image(encode(bomb, "PNG", optimize=True))
    assert caught.value.code == "unsupported_image"


def test_truncated_jpeg_is_unsupported() -> None:
    content = encode(gradient(200, 200), "JPEG")
    with pytest.raises(ImageError) as caught:
        process_image(content[: len(content) // 2])
    assert caught.value.code == "unsupported_image"


@pytest.mark.parametrize(
    ("mode", "fmt", "expected_mode"),
    [
        ("P", "PNG", "RGB"),
        ("CMYK", "JPEG", "RGB"),
        ("L", "WEBP", "RGB"),
        ("LA", "PNG", "LA"),
        ("1", "PNG", "L"),
    ],
)
def test_modes_are_normalised_per_format(mode: str, fmt: str, expected_mode: str) -> None:
    source = Image.new(mode, (8, 8))
    result = process_image(encode(source, fmt))
    assert decode(result).mode == expected_mode
