"""The storefront landing page (``GET home/``, feat/live-landing): the numbers, the spotlight, the rails, the
cashback tiers, online selling off and a bounded query count."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.customers.tests.p1_support import app_client
from glowderma.loyalty.models import CashbackPolicy
from glowderma.professionals.tests.factories import LocationFactory, ProfessionalFactory

pytestmark = pytest.mark.django_db
HOME = "/api/v1/home/"
T0 = datetime(2026, 11, 2, 0, 30, tzinfo=TASHKENT)  # hour 0: the spotlight is not rotated
CARD_KEYS = {
    "id",
    "slug",
    "name",
    "content_language",
    "sku",
    "volume",
    "price",
    "promo",
    "category",
    "main_image_url",
    "main_image_thumbnail_url",
    "main_image_medium_url",
    "available",
    "stock_status",
    "in_cart_quantity",
    "updated_at",
}


@pytest.fixture(autouse=True)
def no_bestsellers() -> Iterator[None]:
    cache.delete(commerce_selectors.BESTSELLERS_CACHE_KEY)
    yield
    cache.delete(commerce_selectors.BESTSELLERS_CACHE_KEY)


@pytest.fixture
def at_hour(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    state = {"now": T0}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(hour: int) -> None:
        state["now"] = T0.replace(hour=hour)

    return move


def product(
    minutes_ago: int, *, image: bool = True, quantity: int = 10, promo: bool = False, **fields: Any
) -> Product:
    """A visible product created ``minutes_ago`` before T0 (older = smaller rank in ``new``)."""
    row = ProductFactory.create(created_at=T0 - timedelta(minutes=minutes_ago), promo=promo, **fields)
    ProductStockFactory.create(product=row, quantity=quantity)
    if image:
        ProductImageFactory.create(product=row)
    return row


def spotlight(body: dict[str, Any]) -> list[tuple[str, int]]:
    return [(item["reason"], item["product"]["id"]) for item in body["spotlight"]]


def rails(body: dict[str, Any]) -> dict[str, list[int]]:
    return {rail["key"]: [card["id"] for card in rail["items"]] for rail in body["rails"]}


def test_empty_storefront(at_hour: Callable[[int], None]) -> None:
    response = app_client().get(HOME)
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "public, max-age=60"
    assert response.json() == {
        "content_language": "uz",
        "online_sales_enabled": True,
        "stats": {"products": 0, "products_in_stock": 0, "categories": 0, "specialists": 0, "clinics": 0},
        "spotlight": [],
        "rails": [],
        "cashback": None,
    }


def test_numbers_rails_and_spotlight(at_hour: Callable[[int], None]) -> None:
    newest = [product(minutes) for minutes in range(1, 15)]  # 14 products, newest first
    promo_a = product(30, promo=True)
    promo_b = product(31, promo=True, image=False)  # no image: never in the spotlight
    low = product(40, quantity=3)
    product(50, quantity=0)
    ProductFactory.create(status=enums.ProductStatus.HIDDEN, created_at=T0)  # not visible
    CategoryFactory.create(is_active=False)
    location = LocationFactory.create()
    LocationFactory.create(is_active=False)
    ProfessionalFactory.create(location=location)
    ProfessionalFactory.create(location=location, is_active=False)

    body = app_client(HTTP_ACCEPT_LANGUAGE="ru").get(HOME).json()
    assert body["content_language"] == "ru"
    visible = Product.objects.exclude(status=enums.ProductStatus.HIDDEN).count()
    assert body["stats"] == {
        "products": visible,
        "products_in_stock": visible - 1,
        "categories": visible + 1,  # one category per product (the factory), the hidden product's too
        "specialists": 1,
        "clinics": 1,
    }
    assert rails(body) == {
        "new": [row.pk for row in newest[:12]],
        "promo": [promo_a.pk, promo_b.pk],
    }
    assert [rail["key"] for rail in body["rails"]] == ["new", "promo"]
    assert set(body["rails"][0]["items"][0]) == CARD_KEYS
    assert spotlight(body) == [
        ("promo", promo_a.pk),
        ("low_stock", low.pk),
        ("new", newest[0].pk),
        ("new", newest[1].pk),
        ("new", newest[2].pk),
        ("new", newest[3].pk),
    ]
    low_card = next(item["product"] for item in body["spotlight"] if item["reason"] == "low_stock")
    assert low_card["available"] == 3


def test_spotlight_rotates_with_the_tashkent_hour(at_hour: Callable[[int], None]) -> None:
    rows = [product(minutes) for minutes in range(1, 5)]  # 4 spotlight items
    at_hour(0)
    first = spotlight(app_client().get(HOME).json())
    assert [pk for _, pk in first] == [row.pk for row in rows]
    at_hour(6)  # 6 mod 4 = 2
    assert spotlight(app_client().get(HOME).json()) == first[2:] + first[:2]


def test_bestsellers_come_from_the_cache_only(at_hour: Callable[[int], None]) -> None:
    rows = [product(minutes) for minutes in range(1, 7)]
    hidden = ProductFactory.create(status=enums.ProductStatus.HIDDEN)
    best = [rows[5].pk, hidden.pk, rows[4].pk, rows[3].pk, 999_999]
    cache.set(commerce_selectors.BESTSELLERS_CACHE_KEY, best)
    body = app_client().get(HOME).json()
    assert "bestsellers" not in rails(body)  # three visible: below the rail's minimum of four
    assert spotlight(body)[:3] == [
        ("bestseller", rows[5].pk),
        ("bestseller", rows[4].pk),
        ("bestseller", rows[3].pk),
    ]
    cache.set(commerce_selectors.BESTSELLERS_CACHE_KEY, [*best, rows[0].pk])
    body = app_client().get(HOME).json()
    assert [rail["key"] for rail in body["rails"]] == ["new", "bestsellers"]
    assert rails(body)["bestsellers"] == [rows[5].pk, rows[4].pk, rows[3].pk, rows[0].pk]
    assert len({pk for _, pk in spotlight(body)}) == len(spotlight(body)) == 6


def test_online_sales_off(at_hour: Callable[[int], None], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_modes, "online_sales_enabled", lambda: False)
    product(1, quantity=2)
    body = app_client().get(HOME).json()
    assert body["online_sales_enabled"] is False
    assert body["stats"]["products_in_stock"] is None
    assert [reason for reason, _ in spotlight(body)] == ["new"]
    assert body["rails"][0]["items"][0]["available"] == 0


def test_cashback_tiers(policies: dict[int, CashbackPolicy]) -> None:
    cashback = app_client().get(HOME).json()["cashback"]
    assert cashback["min_percent"] == "1.00" and cashback["max_percent"] == "3.00"
    assert cashback["tiers"][0] == {"from_amount": 0, "to_amount": 20_000_000, "percent": "1.00"}
    assert [tier["percent"] for tier in cashback["tiers"]] == ["1.00", "2.00", "3.00"]


def test_query_count_is_bounded(at_hour: Callable[[int], None]) -> None:
    def cost() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert app_client().get(HOME).status_code == 200
        return len(captured.captured_queries)

    for minutes in range(1, 4):
        product(minutes, promo=minutes == 1, quantity=minutes)
    cache.set(commerce_selectors.BESTSELLERS_CACHE_KEY, [row.pk for row in Product.objects.all()])
    small = cost()
    for minutes in range(10, 40):
        product(minutes, promo=minutes % 3 == 0, quantity=minutes % 6)
    cache.set(commerce_selectors.BESTSELLERS_CACHE_KEY, [row.pk for row in Product.objects.all()][:12])
    large = cost()
    assert large == small
    assert large <= 20
