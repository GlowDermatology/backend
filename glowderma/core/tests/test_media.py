from hashlib import sha256

import pytest
from django.test import override_settings

from glowderma.core.media import absolute_media_url, media_name


@pytest.mark.parametrize("base", ["https://media.example.uz/", "https://media.example.uz"])
def test_absolute_media_url_joins_without_double_slashes(base: str) -> None:
    """Contract 1.4: media URLs "built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host"."""
    with override_settings(PUBLIC_MEDIA_BASE_URL=base):
        assert absolute_media_url("products/1/9f2c.webp") == "https://media.example.uz/products/1/9f2c.webp"
        assert absolute_media_url("/products/1/9f2c.webp") == "https://media.example.uz/products/1/9f2c.webp"


def test_default_media_base_is_local_dev_server() -> None:
    assert absolute_media_url("x.png") == "http://127.0.0.1:8000/media/x.png"


def test_media_name_is_content_addressed() -> None:
    content = b"image bytes"
    digest = sha256(content).hexdigest()[:16]
    assert media_name("products", 1, content, "webp") == f"products/1/{digest}.webp"
    assert media_name("banners", "abc", content, "jpg") == f"banners/abc/{digest}.jpg"
    assert media_name("products", 1, b"other", "webp") != media_name("products", 1, content, "webp")
