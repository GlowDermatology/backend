"""Sales dashboard reads of ``commerce``: paid orders by day and channel, the status funnel, top products."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from glowderma.catalog.tests.factories import ProductFactory
from glowderma.commerce import selectors
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db
DAY = datetime(2026, 10, 30, 0, 0, tzinfo=TASHKENT)


def test_paid_orders_by_day_and_channel(django_assert_num_queries: Any) -> None:
    OrderFactory.create(paid=True, channel="web", created_at=DAY + timedelta(hours=8))
    OrderFactory.create(paid=True, channel="web", created_at=DAY + timedelta(hours=9))
    OrderFactory.create(delivered=True, channel="android", created_at=DAY + timedelta(days=1, hours=1))
    OrderFactory.create(channel="web", created_at=DAY + timedelta(hours=10))  # awaiting payment: not paid
    OrderFactory.create(
        paid=True, channel="web", created_at=DAY - timedelta(hours=2)
    )  # paid before the window
    with django_assert_num_queries(1):
        rows = selectors.paid_orders_by_day_and_channel(DAY, DAY + timedelta(days=2))
    assert rows == {(date(2026, 10, 30), "web"): 2, (date(2026, 10, 31), "android"): 1}


def test_status_breakdown_counts_every_status_of_the_created_orders(django_assert_num_queries: Any) -> None:
    OrderFactory.create(created_at=DAY + timedelta(hours=1))
    OrderFactory.create(paid=True, created_at=DAY + timedelta(hours=2))
    OrderFactory.create(paid=True, created_at=DAY + timedelta(hours=3))
    OrderFactory.create(cancelled=True, created_at=DAY + timedelta(hours=4))
    OrderFactory.create(paid=True, created_at=DAY - timedelta(hours=4))  # outside
    with django_assert_num_queries(1):
        counts = selectors.status_breakdown(DAY, DAY + timedelta(days=1))
    assert counts == {
        "awaiting_payment": 1,
        "processing": 2,
        "handed_to_courier": 0,
        "delivered": 0,
        "cancelled": 1,
    }


def test_top_products_rank_paid_lines_by_revenue(django_assert_num_queries: Any) -> None:
    serum, cream, mask = (ProductFactory.create(price=price) for price in (100_000, 60_000, 500_000))

    def order(*lines: tuple[Any, int], **traits: Any) -> None:
        items = [
            {"product": product, "quantity": quantity, "unit_price": product.price}
            for product, quantity in lines
        ]
        subtotal = sum(product.price * quantity for product, quantity in lines)
        OrderFactory.create(
            items=items, items_subtotal=subtotal, created_at=DAY + timedelta(hours=9), **traits
        )

    order((serum, 2), (cream, 1), paid=True)
    order((cream, 3), delivered=True)
    order((mask, 5), cancelled=True)  # refunded: never revenue
    order((mask, 1))  # unpaid
    with django_assert_num_queries(1):
        top = selectors.top_products(DAY, DAY + timedelta(days=1), limit=10)
    assert [(row.product_id, row.quantity, row.revenue, row.orders) for row in top] == [
        (cream.pk, 4, 240_000, 2),
        (serum.pk, 2, 200_000, 1),
    ]
    assert selectors.top_products(DAY, DAY + timedelta(days=1), limit=1)[0].product_id == cream.pk
