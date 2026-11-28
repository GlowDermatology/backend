"""``commerce.storefront``: a checkout's stock reservation, its release or conversion, and a new bestsellers
list change what the website shows."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache

from glowderma.commerce import selectors, services, tasks
from glowderma.commerce.models import StockReservation
from glowderma.commerce.tests.factories import StockReservationFactory
from glowderma.core import timeutils
from glowderma.core.tests.storefront_support import revalidated

pytestmark = pytest.mark.django_db


def test_a_reservation_and_its_release(django_capture_on_commit_callbacks: Any) -> None:
    capture = django_capture_on_commit_callbacks
    holder: dict[str, Any] = {}

    def reserve() -> None:
        holder["reservation"] = StockReservationFactory.create()

    assert "shop:products" in revalidated(capture, reserve)
    order = holder["reservation"].order

    def release() -> int:
        return services._settle_reservations(
            order,
            state=StockReservation.State.RELEASED,
            released_at=timeutils.now(),
            release_reason=StockReservation.ReleaseReason.EXPIRED,
            updated_at=timeutils.now(),
        )

    assert revalidated(capture, release) == {"shop:products"}
    assert revalidated(capture, release) == set()  # nothing active left: nothing changed


def test_a_new_bestsellers_list(django_capture_on_commit_callbacks: Any) -> None:
    capture = django_capture_on_commit_callbacks
    cache.delete(selectors.BESTSELLERS_CACHE_KEY)
    assert revalidated(capture, tasks.refresh_bestsellers) == {"shop:home"}
    assert revalidated(capture, tasks.refresh_bestsellers) == set()  # the same list: nothing to refresh
    cache.delete(selectors.BESTSELLERS_CACHE_KEY)
