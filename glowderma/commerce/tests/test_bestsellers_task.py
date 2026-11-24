"""The storefront bestsellers (feat/live-landing): ``commerce.refresh_bestsellers`` caches the 30-day revenue
ranking hourly; ``cached_bestseller_ids`` reads it and never computes it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache

from config.celery import app
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.commerce import selectors, tasks
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import timeutils
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TASHKENT)


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    cache.delete(selectors.BESTSELLERS_CACHE_KEY)
    yield
    cache.delete(selectors.BESTSELLERS_CACHE_KEY)


def paid(product: Any, quantity: int, created_at: datetime) -> None:
    OrderFactory.create(
        items=[{"product": product, "quantity": quantity, "unit_price": product.price}],
        items_subtotal=product.price * quantity,
        created_at=created_at,
        paid=True,
    )


def test_refresh_caches_the_30_day_ranking() -> None:
    serum, cream, old = (ProductFactory.create(price=price) for price in (100_000, 60_000, 900_000))
    paid(serum, 1, NOW - timedelta(days=2))
    paid(cream, 3, NOW - timedelta(days=29))
    paid(old, 5, NOW - timedelta(days=31))  # outside the window
    assert selectors.cached_bestseller_ids() == []
    with mock.patch.object(timeutils, "now", lambda: NOW):
        assert tasks.refresh_bestsellers.delay().get() == [cream.pk, serum.pk]
    assert selectors.cached_bestseller_ids() == [cream.pk, serum.pk]


def test_cached_ids_ignore_a_foreign_value() -> None:
    cache.set(selectors.BESTSELLERS_CACHE_KEY, "nope")
    assert selectors.cached_bestseller_ids() == []
    cache.set(selectors.BESTSELLERS_CACHE_KEY, [3, "4", True, 5])
    assert selectors.cached_bestseller_ids() == [3, 5]


def test_refresh_is_scheduled_hourly() -> None:
    app.autodiscover_tasks(force=True)
    assert "commerce.refresh_bestsellers" in app.tasks
    assert app.conf.beat_schedule["commerce-refresh-bestsellers"] == {
        "task": "commerce.refresh_bestsellers",
        "schedule": 3600.0,
    }
