from hashlib import sha256

import pytest
from django.test import override_settings

from glowderma.core.media import VARIANT_SIDES, absolute_media_url, media_name, variant_name, variant_url


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


def test_variant_name_replaces_extension() -> None:
    assert (
        variant_name("products/12/d5872db9aa73d5e9.jpg", "thumbnail")
        == "products/12/d5872db9aa73d5e9_400.webp"
    )
    assert (
        variant_name("products/12/d5872db9aa73d5e9.jpg", "medium") == "products/12/d5872db9aa73d5e9_1000.webp"
    )


def test_variant_name_keeps_collision_suffix() -> None:
    assert variant_name("products/4/98967745a01b7d3b_mkY6nkg.webp", "thumbnail") == (
        "products/4/98967745a01b7d3b_mkY6nkg_400.webp"
    )


def test_variant_name_works_on_a_url_and_touches_only_the_last_segment() -> None:
    assert variant_name("https://glow.example/media/v1.2/banners/1/8349b8f7b347589a.png", "medium") == (
        "https://glow.example/media/v1.2/banners/1/8349b8f7b347589a_1000.webp"
    )


def test_variant_name_without_extension() -> None:
    assert variant_name("customers/7/abc", "thumbnail") == "customers/7/abc_400.webp"


def test_variant_sides() -> None:
    assert VARIANT_SIDES == {"thumbnail": 400, "medium": 1000}


def test_variant_url() -> None:
    with override_settings(PUBLIC_MEDIA_BASE_URL="https://glow.example/media/"):
        assert (
            variant_url("categories/3/aa.png", "thumbnail")
            == "https://glow.example/media/categories/3/aa_400.webp"
        )
