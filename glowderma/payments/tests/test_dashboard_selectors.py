"""``payments.selectors.revenue_by_day_and_channel``: the sales chart's net revenue by channel."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.payments import selectors
from glowderma.payments.models import LATE_CAPTURE
from glowderma.payments.tests.factories import OrderRowFactory, PaymentFactory, RefundFactory

pytestmark = pytest.mark.django_db
DAY = datetime(2026, 10, 30, 0, 0, tzinfo=TASHKENT)


def _paid(confirmed: datetime, amount: int, channel: str = enums.OrderChannel.IOS, **extra: Any) -> Any:
    order = OrderRowFactory.create(paid=True, channel=channel, created_at=confirmed - timedelta(minutes=3))
    return PaymentFactory.create(order=order, amount=amount, confirmed_at=confirmed, **extra)


def test_revenue_is_split_by_day_and_channel_net_of_refunds(django_assert_num_queries: Any) -> None:
    _paid(DAY + timedelta(hours=10), 100_000, enums.OrderChannel.WEB)
    _paid(DAY + timedelta(hours=11), 50_000, enums.OrderChannel.WEB)
    _paid(DAY + timedelta(hours=23, minutes=59), 70_000, enums.OrderChannel.ANDROID)
    refunded = _paid(DAY + timedelta(days=1, hours=1), 40_000, enums.OrderChannel.IOS)
    RefundFactory.create(payment=refunded, amount=40_000, succeeded=True)
    _paid(DAY + timedelta(hours=9), 30_000, error_code=LATE_CAPTURE)  # never order revenue
    _paid(DAY - timedelta(minutes=1), 999_000)  # before the window
    with django_assert_num_queries(1):
        revenue = selectors.revenue_by_day_and_channel(DAY, DAY + timedelta(days=2))
    assert revenue == {
        (date(2026, 10, 30), "web"): 150_000,
        (date(2026, 10, 30), "android"): 70_000,
        (date(2026, 10, 31), "ios"): 0,
    }
