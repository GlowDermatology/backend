"""``GET home/`` with sections staff filled by hand (admin «Bosh sahifa», §2.21): the picks show in their
order, a pick that cannot show is skipped at read time (hidden, no photo, promo ended, inactive category,
unpublished specialist), a section left with nothing is automatic again, and the query count stays bounded."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glowderma.accounts.models import StaffUser
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.customers.tests.p1_support import app_client
from glowderma.professionals import services as professional_services
from glowderma.professionals.tests.factories import ProfessionalFactory

pytestmark = pytest.mark.django_db
HOME = "/api/v1/home/"
T0 = datetime(2026, 11, 2, 5, 30, tzinfo=TASHKENT)  # hour 5: an automatic spotlight would be rotated


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(timeutils, "now", lambda: T0)
    cache.delete(commerce_selectors.BESTSELLERS_CACHE_KEY)
    yield
    cache.delete(commerce_selectors.BESTSELLERS_CACHE_KEY)


def product(minutes_ago: int, *, image: bool = True, promo: bool = False, **fields: Any) -> Product:
    row = ProductFactory.create(created_at=T0 - timedelta(minutes=minutes_ago), promo=promo, **fields)
    ProductStockFactory.create(product=row, quantity=10)
    if image:
        ProductImageFactory.create(product=row)
    return row


def home() -> dict[str, Any]:
    response = app_client().get(HOME)
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def pick(staff: StaffUser, section: str, rows: list[Any]) -> None:
    refs = [row.slug if section == enums.HomeSectionKey.CATEGORIES else str(row.pk) for row in rows]
    if section == enums.HomeSectionKey.SPECIALISTS:
        professional_services.set_home_specialists(refs, actor=staff)
    else:
        catalog_services.set_home_picks(section, refs, actor=staff)


def frames(body: dict[str, Any]) -> list[tuple[str | None, int]]:
    return [(frame["reason"], frame["product"]["id"]) for frame in body["spotlight"]]


def rail(body: dict[str, Any], key: str) -> list[int] | None:
    return next(([card["id"] for card in r["items"]] for r in body["rails"] if r["key"] == key), None)


def test_a_picked_spotlight_keeps_its_order_and_says_only_true_reasons(staff: StaffUser) -> None:
    newest = product(1)
    on_promo = product(2, promo=True)
    older = [product(100 + n) for n in range(12)]  # pushes `plain` out of the newest 12
    plain = product(500)
    pick(staff, enums.HomeSectionKey.SPOTLIGHT, [plain, on_promo, newest])
    assert frames(home()) == [(None, plain.pk), ("promo", on_promo.pk), ("new", newest.pk)]
    assert older  # the automatic spotlight would have rotated by the hour; the picked one never does


def test_a_spotlight_pick_that_cannot_show_is_skipped_and_none_left_is_automatic(staff: StaffUser) -> None:
    first, second = product(1), product(2)
    pick(staff, enums.HomeSectionKey.SPOTLIGHT, [second, first])
    Product.objects.filter(pk=second.pk).update(status=enums.ProductStatus.HIDDEN)
    assert frames(home()) == [("new", first.pk)]
    first.images.all().delete()
    automatic = frames(home())
    assert automatic == []  # no product with a photo is left: the automatic spotlight is empty too


def test_picked_rails_show_in_order_and_the_feed_then_starts_at_the_newest(staff: StaffUser) -> None:
    rows = [product(n, promo=n % 2 == 0) for n in range(1, 7)]
    automatic = home()
    assert automatic["feed_cursor"] is not None
    pick(staff, enums.HomeSectionKey.NEW, [rows[4], rows[0]])
    pick(staff, enums.HomeSectionKey.PROMO, [rows[1], rows[5]])
    pick(staff, enums.HomeSectionKey.BESTSELLERS, [rows[2]])  # under the automatic minimum of 4
    body = home()
    assert rail(body, "new") == [rows[4].pk, rows[0].pk]
    assert rail(body, "promo") == [rows[1].pk, rows[5].pk]
    assert rail(body, "bestsellers") == [rows[2].pk]
    assert body["feed_cursor"] is None


def test_a_promo_pick_whose_promotion_ended_is_skipped(staff: StaffUser) -> None:
    first, second = product(1, promo=True), product(2, promo=True)
    pick(staff, enums.HomeSectionKey.PROMO, [second, first])
    Product.objects.filter(pk=second.pk).update(promo=False)
    assert rail(home(), "promo") == [first.pk]
    Product.objects.filter(pk=first.pk).update(promo=False)
    assert rail(home(), "promo") is None  # automatic again, and nothing is on promo


def test_an_emptied_section_is_automatic_again(staff: StaffUser) -> None:
    rows = [product(n) for n in range(1, 4)]
    pick(staff, enums.HomeSectionKey.NEW, [rows[2]])
    assert rail(home(), "new") == [rows[2].pk]
    pick(staff, enums.HomeSectionKey.NEW, [])
    body = home()
    assert rail(body, "new") == [row.pk for row in rows]
    assert body["feed_cursor"] is not None


def test_picked_categories_and_specialists(staff: StaffUser) -> None:
    acne, dry, oily = CategoryFactory.create_batch(3)
    first, second = ProfessionalFactory.create_batch(2)
    assert (home()["categories"], home()["specialists"]) == (None, None)
    pick(staff, enums.HomeSectionKey.CATEGORIES, [oily, acne])
    pick(staff, enums.HomeSectionKey.SPECIALISTS, [second, first])
    body = home()
    assert body["categories"] == [oily.slug, acne.slug]
    assert body["specialists"] == [second.pk, first.pk]
    oily.is_active = False
    oily.save(update_fields=["is_active"])
    second.is_active = False
    second.save(update_fields=["is_active"])
    body = home()
    assert body["categories"] == [acne.slug]
    assert body["specialists"] == [first.pk]
    acne.is_active = False
    acne.save(update_fields=["is_active"])
    first.is_active = False
    first.save(update_fields=["is_active"])
    body = home()
    assert (body["categories"], body["specialists"]) == (None, None)
    assert dry.is_active


def test_query_count_stays_bounded_with_every_section_picked(staff: StaffUser) -> None:
    def cost() -> int:
        with CaptureQueriesContext(connection) as captured:
            home()
        return len(captured.captured_queries)

    def costs(count: int) -> tuple[int, int]:
        """The cost of the automatic landing, then with every section picked, over the same rows."""
        rows = [product(n + 1, promo=True) for n in range(count)]
        categories = CategoryFactory.create_batch(count)
        specialists = ProfessionalFactory.create_batch(count)
        automatic = cost()
        pick(staff, enums.HomeSectionKey.SPOTLIGHT, rows[:6])
        for key in (enums.HomeSectionKey.NEW, enums.HomeSectionKey.PROMO, enums.HomeSectionKey.BESTSELLERS):
            pick(staff, key, rows)
        pick(staff, enums.HomeSectionKey.CATEGORIES, categories)
        pick(staff, enums.HomeSectionKey.SPECIALISTS, specialists)
        picked = cost()
        for key in enums.HomeSectionKey.values:
            pick(staff, key, [])
        return automatic, picked

    small = costs(2)
    large = costs(8)
    assert large == small
    # Picked: one issue check per product section (4), the categories' (1), the specialists' (2).
    assert small[1] - small[0] == 7
