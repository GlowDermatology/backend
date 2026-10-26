"""The public catalog (customer contract §2.3, **O**)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import start_test_session
from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.commerce.tests.factories import CartFactory, CartItemFactory, StockReservationFactory
from glowderma.core import enums, timeutils
from glowderma.customers.tests.p1_support import app_client

pytestmark = pytest.mark.django_db
PRODUCTS = "/api/v1/catalog/products/"
BANNERS = "/api/v1/catalog/banners/"
CATEGORIES = "/api/v1/catalog/categories/"


def ids(response: Any) -> list[int]:
    return [row["id"] for row in response.data["results"]]


def stocked(**fields: Any) -> Any:
    quantity = fields.pop("quantity", 10)
    product = ProductFactory.create(**fields)
    ProductStockFactory.create(product=product, quantity=quantity)
    return product


def test_visibility_and_detail_404() -> None:
    visible = stocked()
    hidden = stocked(status=enums.ProductStatus.HIDDEN)
    cheap = stocked(price=999)
    inactive = stocked(category=CategoryFactory.create(is_active=False))
    response = app_client().get(PRODUCTS)
    assert response.status_code == 200
    assert ids(response) == [visible.pk]
    assert set(response.data) == {"count", "page", "page_size", "page_count", "results"}
    for product in (hidden, cheap, inactive):
        assert app_client().get(f"{PRODUCTS}{product.pk}/").status_code == 404
    detail = app_client().get(f"{PRODUCTS}{visible.pk}/").data
    assert detail["max_quantity"] == 10 and detail["images"] == []


def test_card_shape_language_fallback_and_availability() -> None:
    product = stocked(quantity=3, name_ru="Сыворотка", name_uz="Zardob", name_en="")
    StockReservationFactory.create(product=product, quantity=2)
    ProductImageFactory.create(product=product)

    def card_of(lang: str) -> dict[str, Any]:
        rows = app_client(HTTP_ACCEPT_LANGUAGE=lang).get(PRODUCTS, {"page_size": 50}).data["results"]
        return dict(next(row for row in rows if row["id"] == product.pk))

    card = card_of("ru")
    assert set(card) == {
        "id",
        "name",
        "volume",
        "price",
        "promo",
        "category",
        "main_image_url",
        "available",
        "stock_status",
        "in_cart_quantity",
    }
    assert card["name"] == "Сыворотка" and card["available"] == 1 and card["stock_status"] == "in_stock"
    assert card["main_image_url"].startswith("http")
    assert card_of("en")["name"] == "Zardob"
    bare = stocked(name_uz="", billz_name="BILLZ ONLY 30ML")
    names = {row["id"]: row["name"] for row in app_client().get(PRODUCTS, {"page_size": 50}).data["results"]}
    assert names[bare.pk] == "BILLZ ONLY 30ML"


def test_filters_search_ordering_and_page_size() -> None:
    category = CategoryFactory.create(slug="serums")
    cheap = stocked(price=50_000, name_uz="Arzon krem", name_ru="Дешёвый крем", category=category)
    dear = stocked(price=300_000, name_uz="Qimmat serum", name_en="Premium serum", promo=True)
    empty = stocked(price=100_000, name_uz="Bo‘sh", quantity=0)
    assert ids(app_client().get(PRODUCTS, {"ordering": "price"})) == [cheap.pk, empty.pk, dear.pk]
    assert ids(app_client().get(PRODUCTS, {"ordering": "-price"})) == [dear.pk, empty.pk, cheap.pk]
    assert ids(app_client().get(PRODUCTS)) == [empty.pk, dear.pk, cheap.pk]  # newest first
    assert ids(app_client().get(PRODUCTS, {"ordering": "name"}))[0] == cheap.pk
    assert ids(app_client().get(PRODUCTS, {"search": "дешёв"})) == [cheap.pk]
    assert ids(app_client().get(PRODUCTS, {"search": "premium"})) == [dear.pk]
    assert len(ids(app_client().get(PRODUCTS, {"search": "x"}))) == 3  # under 2 characters: ignored
    assert ids(app_client().get(PRODUCTS, {"category": "serums"})) == [cheap.pk]
    assert ids(app_client().get(PRODUCTS, {"promo": "true"})) == [dear.pk]
    assert set(ids(app_client().get(PRODUCTS, {"in_stock": "true"}))) == {cheap.pk, dear.pk}
    assert ids(app_client().get(PRODUCTS, {"in_stock": "false"})) == [empty.pk]
    bad = app_client().get(PRODUCTS, {"category": "nope"})
    assert bad.data["errors"][0] == {
        "field": "category",
        "code": "invalid_choice",
        "message": "Ruxsat etilmagan qiymat.",
    }
    assert app_client().get(PRODUCTS, {"page_size": 50}).data["page_size"] == 50
    assert app_client().get(PRODUCTS, {"page_size": 30}).data["errors"][0]["field"] == "page_size"


def test_optional_auth_cart_quantity_and_cache_headers() -> None:
    product = stocked()
    anonymous = APIClient().get(PRODUCTS)
    assert anonymous["Cache-Control"] == "public, max-age=30" and anonymous["Vary"] == "Accept-Language"
    customer = Customer.objects.create(phone="+998901010101", name="C")
    CartItemFactory.create(cart=CartFactory.create(customer=customer), product=product, quantity=2)
    started = start_test_session(customer)
    access, _ = tokens.issue_access(started.session)
    signed = app_client(fid="fid-test-0001", HTTP_AUTHORIZATION=f"Bearer {access}").get(PRODUCTS)
    assert signed.status_code == 200
    assert signed.data["results"][0]["in_cart_quantity"] == 2
    assert (
        signed["Cache-Control"] == "private, no-cache" and signed["Vary"] == "Accept-Language, Authorization"
    )
    junk = APIClient().get(PRODUCTS, HTTP_AUTHORIZATION="Bearer junk")
    assert (junk.status_code, junk.data["code"]) == (401, "not_authenticated")


def test_banners_window_and_link_downgrade() -> None:
    now = timeutils.now()
    product = stocked()
    hidden = stocked(status=enums.ProductStatus.HIDDEN)
    category = CategoryFactory.create(slug="masks")
    off = CategoryFactory.create(slug="off", is_active=False)
    a = BannerFactory.create(link_kind="product", link_product=product)
    b = BannerFactory.create(link_kind="product", link_product=hidden)
    c = BannerFactory.create(link_kind="category", link_category=category)
    d = BannerFactory.create(link_kind="category", link_category=off)
    BannerFactory.create(starts_at=now + timedelta(days=1))
    BannerFactory.create(ends_at=now - timedelta(minutes=1))
    BannerFactory.create(is_active=False)
    response = app_client().get(BANNERS)
    assert response["Cache-Control"] == "public, max-age=60"
    links = {row["id"]: row["link"] for row in response.data["results"]}
    assert list(links) == [a.pk, b.pk, c.pk, d.pk]
    assert links[a.pk] == {"kind": "product", "product_id": product.pk, "category": None}
    assert links[b.pk] == {"kind": "none", "product_id": None, "category": None}
    assert links[c.pk] == {"kind": "category", "product_id": None, "category": "masks"}
    assert links[d.pk]["kind"] == "none"


def test_categories_count_visible_products() -> None:
    category = CategoryFactory.create(slug="creams", name_ru="Кремы")
    stocked(category=category)
    stocked(category=category, status=enums.ProductStatus.HIDDEN)
    CategoryFactory.create(slug="hidden", is_active=False)
    rows = app_client(HTTP_ACCEPT_LANGUAGE="ru").get(CATEGORIES).data["results"]
    assert rows == [{"slug": "creams", "name": "Кремы", "image_url": None, "product_count": 1}]


@override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake")
def test_fake_billz_in_production_lists_products_but_sells_nothing() -> None:
    product = stocked(quantity=8)
    response = app_client().get(PRODUCTS)
    card = response.data["results"][0]
    assert (card["id"], card["available"], card["stock_status"]) == (product.pk, 0, "out_of_stock")
    assert app_client().get(f"{PRODUCTS}{product.pk}/").data["max_quantity"] == 0
    assert ids(app_client().get(PRODUCTS, {"in_stock": "true"})) == []
