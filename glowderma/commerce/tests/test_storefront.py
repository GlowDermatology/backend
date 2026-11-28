"""``commerce.storefront``: a checkout's stock reservation, its release or conversion, and a new bestsellers
list change what the website shows."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache

from glowderma.commerce import selectors, services, tasks
from glowderma.commerce.models import StockReservation
from glowderma.commerce.tests.factories import StockReservationFactory
from glowderma.commerce.tests.support import admin, pay, place, seed_policies
from glowderma.core import timeutils
from glowderma.core.tests.storefront_support import revalidated
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import Purchase

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


@pytest.fixture
def policies() -> None:
    seed_policies()


@pytest.mark.usefixtures("policies")
def test_a_checkout_and_its_cancellation(django_capture_on_commit_callbacks: Any) -> None:
    """A checkout reserves its lines with ``bulk_create`` (no model signal): it marks products itself."""
    capture = django_capture_on_commit_callbacks
    holder: dict[str, Any] = {}

    def checkout() -> None:
        holder["order"] = place()

    assert "shop:products" in revalidated(capture, checkout)
    order = holder["order"]
    assert StockReservation.objects.filter(order=order, state=StockReservation.State.ACTIVE).exists()
    staff = admin()
    tags = revalidated(capture, lambda: services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk))
    assert "shop:products" in tags


@pytest.mark.usefixtures("policies")
def test_a_paid_orders_billz_push_and_settlement(django_capture_on_commit_callbacks: Any) -> None:
    """Whether a paid order's units count as sold moves with the Billz push and the settlement."""
    capture = django_capture_on_commit_callbacks
    order = pay(place())
    purchase = Purchase.objects.get(order=order)
    tags = revalidated(
        capture,
        lambda: loyalty_services.link_billz_sale(
            purchase.pk, billz_sale_id="sale-1", billz_sale_ref="S-1", pushed_at=timeutils.now()
        ),
    )
    assert tags == {"shop:products"}
    assert revalidated(capture, lambda: services.settle_order_reservations(order.pk)) == {"shop:products"}
    assert revalidated(capture, lambda: services.settle_order_reservations(order.pk)) == set()
