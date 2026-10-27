"""Contract §2.10 ``GET dashboard/``: ranges and buckets (Tashkent), previous period, revenue net of refunds
(attributed to the payment's bucket), customer stats incl. since-deleted, attention, custom ranges."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import seeding
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.payments.tests.factories import PaymentFactory, RefundFactory
from glowderma.reporting import selectors

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TASHKENT)
URL = "/api/v1/admin/dashboard/"


def _get(client: APIClient, **params: Any) -> Any:
    with seeding.pinned_clock(NOW):
        return client.get(URL, params)


def _paid(confirmed: datetime, amount: int = 100_000, created: datetime | None = None) -> Any:
    order = OrderFactory.create(paid=True, created_at=created or confirmed - timedelta(minutes=3))
    return PaymentFactory.create(order=order, amount=amount, confirmed_at=confirmed)


def test_today_has_24_hour_buckets_and_yesterday_as_previous(staff_client: APIClient) -> None:
    midnight = NOW.replace(hour=0, minute=0, second=0)
    _paid(NOW.replace(hour=9, minute=10), 180_000)
    _paid(midnight, 50_000, created=midnight)  # 00:00:00 belongs to today
    _paid(midnight - timedelta(seconds=1), 70_000, created=midnight - timedelta(seconds=1))  # 23:59:59
    response = _get(staff_client)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert (body["range"], body["from"], body["to"], body["bucket"]) == (
        "today",
        "2026-10-24",
        "2026-10-24",
        "hour",
    )
    assert len(body["buckets"]) == 24
    assert body["buckets"][0] == {"start": "2026-10-24T00:00:00+05:00", "revenue": 50_000, "orders": 1}
    assert body["buckets"][9] == {"start": "2026-10-24T09:00:00+05:00", "revenue": 180_000, "orders": 1}
    assert body["buckets"][23]["start"] == "2026-10-24T23:00:00+05:00"
    assert (body["stats"]["orders"], body["stats"]["revenue"]) == (2, 230_000)
    assert body["previous_period_orders"] == 1
    assert body["generated_at"] == "2026-10-24T12:00:00+05:00"


def test_week_month_and_previous_periods(staff_client: APIClient) -> None:
    for days_ago in (0, 6, 7, 13, 29, 30):
        _paid(NOW - timedelta(days=days_ago), 10_000)
    week = _get(staff_client, range="week").json()
    assert (week["from"], week["to"], week["bucket"], len(week["buckets"])) == (
        "2026-10-18",
        "2026-10-24",
        "day",
        7,
    )
    assert week["buckets"][0]["start"] == "2026-10-18T00:00:00+05:00"
    assert (week["stats"]["orders"], week["previous_period_orders"]) == (2, 2)
    month = _get(staff_client, range="month").json()
    assert (month["from"], len(month["buckets"])) == ("2026-09-25", 30)
    assert (month["stats"]["orders"], month["stats"]["revenue"], month["previous_period_orders"]) == (
        5,
        50_000,
        1,
    )


def test_custom_range_switches_hour_and_day_buckets(staff_client: APIClient) -> None:
    two_days = _get(staff_client, range="custom", **{"from": "2026-10-22", "to": "2026-10-23"}).json()
    assert (two_days["bucket"], len(two_days["buckets"])) == ("hour", 48)
    three_days = _get(staff_client, range="custom", **{"from": "2026-10-21", "to": "2026-10-23"}).json()
    assert (three_days["bucket"], len(three_days["buckets"])) == ("day", 3)
    one_year = _get(staff_client, range="custom", **{"from": "2025-10-24", "to": "2026-10-24"}).json()
    assert len(one_year["buckets"]) == 366


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"range": "custom"}, "from"),
        ({"range": "custom", "from": "2026-10-20"}, "to"),
        ({"range": "custom", "from": "2026-10-21", "to": "2026-10-20"}, "from"),
        ({"range": "custom", "from": "2026-10-20", "to": "2026-10-25"}, "to"),
        ({"range": "custom", "from": "2025-10-23", "to": "2026-10-24"}, "to"),
        ({"range": "year"}, "range"),
    ],
)
def test_custom_range_validation(staff_client: APIClient, params: dict[str, str], field: str) -> None:
    response = _get(staff_client, **params)
    assert response.status_code == 400 and response.json()["errors"][0]["field"] == field


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"range": "custom"}, [("from", "required"), ("to", "required")]),
        ({"range": "custom", "from": "2026-10-21", "to": "2026-10-20"}, [("from", "invalid")]),
        ({"range": "custom", "from": "2026-10-20", "to": "2026-10-25"}, [("to", "invalid")]),
        ({"range": "custom", "from": "2025-10-23", "to": "2026-10-24"}, [("to", "invalid")]),
    ],
)
def test_custom_range_error_codes(
    staff_client: APIClient, params: dict[str, str], expected: list[tuple[str, str]]
) -> None:
    """Contract §2.10: missing → ``required``; from > to → ``invalid`` on from; to > today or an inclusive
    span of more than 366 days (to − from + 1 > 366) → ``invalid`` on to."""
    errors = _get(staff_client, **params).json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == expected


def test_from_and_to_are_ignored_outside_custom(staff_client: APIClient) -> None:
    body = _get(staff_client, range="week", **{"from": "2020-01-01"}).json()
    assert body["from"] == "2026-10-18"
    for params in ({"from": "not-a-date", "to": "2099-01-01"}, {"range": "month", "to": "x"}):
        assert _get(staff_client, **params).status_code == 200  # not even parsed


def test_refunds_are_attributed_to_the_payment_bucket_and_never_negative(staff_client: APIClient) -> None:
    payment = _paid(NOW - timedelta(days=3), 200_000)
    RefundFactory.create(payment=payment, succeeded=True, amount=200_000, requested_at=NOW, completed_at=NOW)
    _paid(NOW.replace(hour=10), 90_000)
    week = _get(staff_client, range="week").json()
    revenues = [bucket["revenue"] for bucket in week["buckets"]]
    assert min(revenues) == 0 and week["stats"]["revenue"] == 90_000
    today = _get(staff_client).json()
    assert today["stats"]["revenue"] == 90_000  # the refund on today does not reduce today


def test_stats_and_attention(staff_client: APIClient) -> None:
    CustomerFactory.create(registered_at=NOW - timedelta(hours=2))
    gone = CustomerFactory.create(registered_at=NOW - timedelta(hours=1))
    account_services.delete_customer(gone)
    CustomerFactory.create(registered_at=NOW - timedelta(days=40))
    ProductStockFactory.create(product=ProductFactory.create(), quantity=0)
    ProductStockFactory.create(product=ProductFactory.create(), quantity=5)
    ProductFactory.create(is_new_from_billz=True, status="hidden")
    ShipmentFactory.create(failed=True, order=OrderFactory.create(paid=True))
    RefundFactory.create(manual_required=True)
    AppointmentFactory.create(start=NOW + timedelta(days=2))
    body = _get(staff_client).json()
    stats = body["stats"]
    assert (stats["new_customers"], stats["deleted_customers"]) == (2, 1)
    assert stats["total_customers"] >= 3
    assert stats["out_of_stock"] >= 1
    attention = body["attention"]
    assert (attention["bts_failed"], attention["refunds_needing_action"], attention["new_from_billz"]) == (
        1,
        1,
        1,
    )
    assert attention["pending_appointments"] == 1 and attention["next_appointment_at"] is not None


def test_attention_flags_a_missing_cashback_policy(
    staff_client: APIClient, django_assert_num_queries: Any
) -> None:
    """Gap CB-9.2.4: true while no policy version is in force at ``now`` (none, or only a future one)."""
    from glowderma.loyalty import selectors as loyalty_selectors
    from glowderma.loyalty.tests.factories import CashbackPolicyFactory

    assert _get(staff_client).json()["attention"]["cashback_policy_missing"] is True
    CashbackPolicyFactory.create(created_at=NOW - timedelta(days=1), effective_from=NOW + timedelta(hours=1))
    assert _get(staff_client).json()["attention"]["cashback_policy_missing"] is True  # scheduled only
    with seeding.pinned_clock(NOW + timedelta(hours=1)):
        assert staff_client.get(URL).json()["attention"]["cashback_policy_missing"] is False
    with seeding.pinned_clock(NOW), django_assert_num_queries(1):  # one query for the flag
        assert loyalty_selectors.cashback_policy_missing(NOW) is True


def test_window_helpers() -> None:
    window = selectors.dashboard_window("custom", date(2026, 10, 24), date(2026, 10, 1), date(2026, 10, 2))
    assert (window.days, window.bucket) == (2, "hour")
    assert window.previous == (datetime(2026, 9, 29, tzinfo=TASHKENT), datetime(2026, 10, 1, tzinfo=TASHKENT))
    with pytest.raises(ValueError):
        selectors.dashboard_window("custom", date(2026, 10, 24))
