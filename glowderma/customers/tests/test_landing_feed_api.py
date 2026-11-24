"""The storefront's endless feed (``GET catalog/feed/``, feat/live-landing): keyset pages of 24 over
``(created_at desc, id desc)``, an opaque cursor, the category filter and the refusals."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import CategoryFactory, ProductFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.customers import selectors
from glowderma.customers.tests.p1_support import app_client

pytestmark = pytest.mark.django_db
FEED = "/api/v1/catalog/feed/"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=TASHKENT)


def make(count: int, *, same_time: bool = False, **fields: Any) -> list[Product]:
    """``count`` visible products, newest last created; ``same_time`` gives them one ``created_at``."""
    return [
        ProductFactory.create(created_at=T0 if same_time else T0 + timedelta(minutes=n), **fields)
        for n in range(count)
    ]


def expected(qs: Any = None) -> list[int]:
    rows = qs if qs is not None else selectors.customer_products()
    return list(rows.order_by("-created_at", "-id").values_list("pk", flat=True))


def walk(params: dict[str, str] | None = None) -> tuple[list[int], int]:
    """Every id the feed returns following ``next_cursor``, and the number of pages."""
    seen: list[int] = []
    query = dict(params or {})
    pages = 0
    while True:
        response = app_client().get(FEED, query)
        assert response.status_code == 200, response.content
        body = response.json()
        assert set(body) == {"results", "next_cursor"}
        assert len(body["results"]) <= selectors.FEED_PAGE_SIZE
        pages += 1
        seen += [card["id"] for card in body["results"]]
        if body["next_cursor"] is None:
            return seen, pages
        query["cursor"] = body["next_cursor"]


def test_feed_pages_through_every_visible_product_once() -> None:
    make(20)
    make(10, same_time=True)  # ties on created_at straddle the page boundary: ordered by id desc
    ProductFactory.create(status=enums.ProductStatus.HIDDEN, created_at=T0 + timedelta(hours=1))
    response = app_client().get(FEED)
    assert response["Cache-Control"] == "public, max-age=30"
    first = response.json()
    assert len(first["results"]) == 24 and first["next_cursor"]
    assert set(first["results"][0]) >= {"id", "slug", "name", "available", "stock_status", "main_image_url"}
    ids, pages = walk()
    assert pages == 2
    assert ids == expected()
    assert len(ids) == len(set(ids)) == 30


def test_feed_exact_page_and_empty() -> None:
    assert app_client().get(FEED).json() == {"results": [], "next_cursor": None}
    make(24)
    body = app_client().get(FEED).json()
    assert len(body["results"]) == 24 and body["next_cursor"] is None


def test_feed_new_products_do_not_shift_the_next_page() -> None:
    make(30)
    first = app_client().get(FEED).json()
    for _ in range(3):  # created after the first page was read
        ProductFactory.create(created_at=T0 + timedelta(hours=1))
    second = app_client().get(FEED, {"cursor": first["next_cursor"]}).json()
    assert [card["id"] for card in second["results"]] == expected()[27:]


def test_feed_category_filter() -> None:
    category = CategoryFactory.create()
    mine = make(3, category=category)
    make(2)
    ids, _ = walk({"category": category.slug})
    assert ids == [row.pk for row in reversed(mine)]
    response = app_client().get(FEED, {"category": "no-such-category"})
    assert response.status_code == 400
    assert [(e["field"], e["code"]) for e in response.json()["errors"]] == [("category", "invalid_choice")]
    hidden = CategoryFactory.create(is_active=False)
    assert app_client().get(FEED, {"category": hidden.slug}).status_code == 400


def encoded(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "not a cursor!",
        "x" * 200,
        encoded("2026-11-02T12:00:00|5"),  # naive time
        encoded("2026-11-02T12:00:00+05:00|0"),  # no such id
        encoded("2026-11-02T12:00:00+05:00|-4"),
        encoded("2026-11-02T12:00:00+05:00"),
        encoded("yesterday|5"),
        "%%%",
    ],
)
def test_feed_refuses_a_foreign_cursor(cursor: str) -> None:
    response = app_client().get(FEED, {"cursor": cursor})
    assert response.status_code == 400, response.content
    assert [(e["field"], e["code"]) for e in response.json()["errors"]] == [("cursor", "invalid")]


def test_cursor_round_trip_and_empty_cursor() -> None:
    cursor = selectors.FeedCursor(created_at=T0, id=42)
    assert selectors.decode_cursor(selectors.encode_cursor(cursor)) == cursor
    make(2)
    assert len(app_client().get(FEED, {"cursor": ""}).json()["results"]) == 2


def test_feed_cursor_past_the_end() -> None:
    make(2)
    oldest = Product.objects.order_by("created_at", "id").first()
    assert oldest is not None
    cursor = selectors.encode_cursor(selectors.FeedCursor(created_at=oldest.created_at, id=oldest.pk))
    assert app_client().get(FEED, {"cursor": cursor}).json() == {"results": [], "next_cursor": None}


def test_feed_query_count_does_not_grow() -> None:
    def cost() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert app_client().get(FEED).status_code == 200
        return len(captured.captured_queries)

    make(3)
    small = cost()
    make(30)
    assert cost() == small
