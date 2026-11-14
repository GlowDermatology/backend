"""Admin API variant URLs (spec 2026-11-13-image-variants): two sibling fields next to every image URL."""

import pytest
from rest_framework.test import APIClient

from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
)
from glowderma.core.media import variant_url

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"


def test_product_and_its_gallery(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    image = ProductImageFactory.create(product=product)
    name = str(image.file.name)
    body = staff_client.get(f"{PRODUCTS}{product.pk}/").json()
    assert body["main_image_thumbnail_url"] == variant_url(name, "thumbnail")
    assert body["main_image_medium_url"] == variant_url(name, "medium")
    assert body["images"][0]["thumbnail_url"] == variant_url(name, "thumbnail")
    assert body["images"][0]["medium_url"] == variant_url(name, "medium")
    listed = staff_client.get(PRODUCTS).json()["results"]
    assert [row["main_image_thumbnail_url"] for row in listed] == [variant_url(name, "thumbnail")]


def test_product_without_images(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    body = staff_client.get(f"{PRODUCTS}{product.pk}/").json()
    assert body["main_image_thumbnail_url"] is None and body["main_image_medium_url"] is None


def test_category(staff_client: APIClient) -> None:
    CategoryFactory.create(slug="plain", name_uz="Oddiy")
    body = staff_client.get("/api/v1/admin/categories/plain/").json()
    assert body["image_thumbnail_url"] is None and body["image_medium_url"] is None
    staff_client.patch(
        "/api/v1/admin/categories/plain/",
        {"image": _png()},
        format="multipart",
    )
    body = staff_client.get("/api/v1/admin/categories/plain/").json()
    name = body["image_url"].split("/media/", 1)[1]
    assert body["image_thumbnail_url"] == variant_url(name, "thumbnail")
    assert body["image_medium_url"] == variant_url(name, "medium")


def test_banner(staff_client: APIClient) -> None:
    banner = BannerFactory.create()
    body = staff_client.get(f"/api/v1/admin/banners/{banner.pk}/").json()
    assert body["image_thumbnail_url"] == variant_url(str(banner.image.name), "thumbnail")
    assert body["image_medium_url"] == variant_url(str(banner.image.name), "medium")


def _png() -> object:
    from glowderma.catalog.tests.helpers import upload

    return upload("PNG")
