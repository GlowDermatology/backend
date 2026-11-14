"""Customer API variant URLs (spec 2026-11-13-image-variants): two sibling fields next to every image URL."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.catalog.tests.helpers import upload
from glowderma.commerce.tests.support import place, product
from glowderma.core.media import variant_name, variant_url
from glowderma.customers.tests.p1_support import app_client
from glowderma.notifications.tests.factories import InboxNotificationFactory
from glowderma.professionals import services as professional_services
from glowderma.professionals.tests.factories import ProfessionalFactory

pytestmark = pytest.mark.django_db


def siblings(body: dict[str, Any], prefix: str) -> tuple[Any, Any]:
    key = f"{prefix}_" if prefix else ""
    return body[f"{key}thumbnail_url"], body[f"{key}medium_url"]


def expected(name: str) -> tuple[str, str]:
    return variant_url(name, "thumbnail"), variant_url(name, "medium")


def test_product_card_and_detail() -> None:
    item = ProductFactory.create()
    ProductStockFactory.create(product=item, quantity=5)
    image = ProductImageFactory.create(product=item)
    name = str(image.file.name)
    card = next(
        row for row in app_client().get("/api/v1/catalog/products/").data["results"] if row["id"] == item.pk
    )
    assert siblings(card, "main_image") == expected(name)
    detail = app_client().get(f"/api/v1/catalog/products/{item.pk}/").data
    assert siblings(detail, "main_image") == expected(name)
    assert siblings(detail["images"][0], "") == expected(name)


def test_product_card_without_an_image() -> None:
    item = ProductFactory.create()
    ProductStockFactory.create(product=item, quantity=5)
    card = next(
        row for row in app_client().get("/api/v1/catalog/products/").data["results"] if row["id"] == item.pk
    )
    assert siblings(card, "main_image") == (None, None)


def test_banner_and_category() -> None:
    banner = BannerFactory.create()
    row = next(
        r for r in app_client().get("/api/v1/catalog/banners/").data["results"] if r["id"] == banner.pk
    )
    assert siblings(row, "image") == expected(str(banner.image.name))
    CategoryFactory.create(slug="plain", name_uz="Oddiy")
    rows = app_client().get("/api/v1/catalog/categories/").data["results"]
    assert siblings(next(r for r in rows if r["slug"] == "plain"), "image") == (None, None)


def test_me_and_the_image_upload(customer_client: APIClient) -> None:
    assert siblings(customer_client.get("/api/v1/me/").json(), "image") == (None, None)
    buffer = BytesIO()
    Image.new("RGB", (640, 480), "red").save(buffer, "PNG")
    part = SimpleUploadedFile("me.png", buffer.getvalue(), content_type="image/png")
    body = customer_client.put("/api/v1/me/image/", {"file": part}, format="multipart").json()
    assert set(body) == {"image_url", "image_thumbnail_url", "image_medium_url"}
    me = customer_client.get("/api/v1/me/").json()
    assert (body["image_thumbnail_url"], body["image_medium_url"]) == siblings(me, "image")
    assert me["image_thumbnail_url"] == variant_name(me["image_url"], "thumbnail")


def test_cart_line(customer_client: APIClient) -> None:
    item = product(stock=5, image=True)
    body = customer_client.post(
        "/api/v1/cart/items/",
        {"product_id": item.pk, "quantity": 1},
        format="json",
        HTTP_IDEMPOTENCY_KEY="9d7c4e2a-0b8f-4c1d-8e5a-1234567890ab",
    ).json()
    assert siblings(body["items"][0], "main_image") == expected(str(item.images.get().file.name))


def test_order_detail_items_and_list_previews(customer: Customer, customer_client: APIClient) -> None:
    item = product(150_000, image=True)
    order = place(customer, [(item, 1)], fulfilment="pickup")
    path = order.items.get().image_url
    detail = customer_client.get(f"/api/v1/me/orders/{order.pk}/").json()
    assert siblings(detail["items"][0], "image") == expected(path)
    row = next(r for r in customer_client.get("/api/v1/me/orders/").json()["results"] if r["id"] == order.pk)
    assert row["preview_thumbnail_images"] == [variant_url(path, "thumbnail")]


def test_specialist_photo() -> None:
    professional = ProfessionalFactory.create()
    professional_services.store_photo(professional, upload("PNG"))
    professional.refresh_from_db()
    rows = app_client().get("/api/v1/specialists/").data["results"]
    row = next(r for r in rows if r["id"] == professional.pk)
    assert siblings(row, "photo") == expected(str(professional.photo.name))


def test_inbox_image(customer: Customer, customer_client: APIClient) -> None:
    url = "https://glow.example/media/campaigns/3/aaaaaaaaaaaaaaaa.png"
    InboxNotificationFactory.create(customer=customer, image_url=url)
    InboxNotificationFactory.create(customer=customer, image_url="")
    rows = customer_client.get("/api/v1/me/notifications/").json()["results"]
    by_image = {row["image_url"]: row for row in rows}
    assert siblings(by_image[url], "image") == (variant_name(url, "thumbnail"), variant_name(url, "medium"))
    assert siblings(by_image[None], "image") == (None, None)
