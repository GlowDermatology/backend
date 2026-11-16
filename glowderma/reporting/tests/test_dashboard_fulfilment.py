"""``GET dashboard/`` order-state counters (Yandex Delivery plan §3.10): ``fulfilment`` over a mixed
dataset, ``attention.yandex_failing``, ``nav-counts.delivery_issues_open`` and the console badge; fixed
query counts."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.commerce import delivery_admin
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import seeding
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery.tests.factories import DeliveryIssueFactory, ShipmentFactory, yandex_shipment
from glowderma.integrations.models import IntegrationState
from glowderma.reporting import admin_nav, selectors

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 15, 15, 0, tzinfo=TASHKENT)
URL = "/api/v1/admin/dashboard/"
EMPTY = {
    "paid_to_prepare": 0,
    "to_hand_to_courier": 0,
    "in_delivery": 0,
    "returning": 0,
    "completed_today": 0,
    "needs_action": 0,
    "urgent": 0,
}


def _yandex(stage: str, **order: Any) -> Any:
    placed = OrderFactory.create(created_at=NOW - timedelta(hours=3), **order)
    created = stage not in ("", "awaiting_dispatch")
    return yandex_shipment(state="created" if created else "not_created", stage=stage, order=placed)


def _mixed() -> None:
    earlier = NOW - timedelta(hours=3)
    # to prepare: pickup, BTS, Yandex awaiting / failed (a new dispatch)
    OrderFactory.create(paid=True, pickup=True, created_at=earlier)
    ShipmentFactory.create(order=OrderFactory.create(paid=True, created_at=earlier))
    _yandex("awaiting_dispatch", paid=True)
    failed = _yandex("failed", paid=True)
    # to hand to the courier
    _yandex("courier_requested", paid=True)
    _yandex("courier_arrived", paid=True)
    # in delivery: BTS and Yandex handed over
    ShipmentFactory.create(order=OrderFactory.create(handed=True, created_at=earlier))
    _yandex("in_transit", handed=True)
    # returning: still open orders only
    _yandex("returning", handed=True)
    _yandex("returned", handed=True)
    _yandex("returning", cancelled=True)  # closed: not counted
    # completed today (Tashkent) — a pickup hand-over and a Yandex delivery; yesterday's is not
    OrderFactory.create(
        delivered=True, pickup=True, created_at=earlier, delivered_at=NOW - timedelta(hours=1)
    )
    _yandex("delivered", delivered=True, delivered_at=NOW.replace(hour=0, minute=5))
    OrderFactory.create(
        delivered=True, created_at=earlier, delivered_at=NOW.replace(hour=0) - timedelta(minutes=5)
    )
    # nothing for unpaid orders
    OrderFactory.create(created_at=earlier)
    # the queue: two open (one urgent), one resolved
    DeliveryIssueFactory.create(order=failed.order)
    DeliveryIssueFactory.create(order=failed.order, kind="parcel_unaccounted", urgent=True)
    DeliveryIssueFactory.create(order=failed.order, kind="incident", state="resolved", resolved_at=NOW)


def test_the_counters_over_a_mixed_dataset(django_assert_num_queries: Any) -> None:
    with seeding.pinned_clock(NOW):
        assert delivery_admin.fulfilment_counts() == EMPTY
        _mixed()
        with django_assert_num_queries(2):  # the orders aggregate + the queue counts
            counts = delivery_admin.fulfilment_counts()
    assert counts == {
        "paid_to_prepare": 4,
        "to_hand_to_courier": 2,
        "in_delivery": 2,
        "returning": 2,
        "completed_today": 2,
        "needs_action": 2,
        "urgent": 1,
    }


def test_the_dashboard_carries_fulfilment_and_yandex_failing(staff_client: APIClient) -> None:
    with seeding.pinned_clock(NOW):
        _mixed()
        body = staff_client.get(URL).json()
    assert body["fulfilment"]["paid_to_prepare"] == 4 and body["fulfilment"]["urgent"] == 1
    assert body["attention"]["yandex_failing"] is False
    # the BTS counters stay BTS-only: the failed Yandex dispatch is not a BTS failure
    assert body["attention"]["bts_failed"] == 0


def test_yandex_failing_is_false_in_fake_mode_and_reads_the_state_in_live(
    django_assert_num_queries: Any,
) -> None:
    IntegrationState.objects.filter(provider="yandex").update(consecutive_failures=3)
    with django_assert_num_queries(0):
        assert delivery_admin.yandex_failing() is False
    with override_settings(YANDEX_DELIVERY_MODE="live"):
        with django_assert_num_queries(1):
            assert delivery_admin.yandex_failing() is True
        IntegrationState.objects.filter(provider="yandex").update(consecutive_failures=0)
        assert delivery_admin.yandex_failing() is False


def test_the_dashboard_query_count_does_not_grow_with_the_orders(staff_client: APIClient) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    window = selectors.dashboard_window("today", NOW.date())
    with seeding.pinned_clock(NOW):
        _mixed()
        with CaptureQueriesContext(connection) as first:
            selectors.dashboard(window, NOW)
        _mixed()
        with CaptureQueriesContext(connection) as second:
            selectors.dashboard(window, NOW)
    assert len(first) == len(second)


def test_nav_counts_and_the_console_badge_count_open_issues(staff_client: APIClient) -> None:
    DeliveryIssueFactory.create()
    DeliveryIssueFactory.create(urgent=True, kind="incident")
    DeliveryIssueFactory.create(kind="dispatch_review", state="resolved", resolved_at=NOW)
    assert staff_client.get("/api/v1/admin/nav-counts/").json()["delivery_issues_open"] == 2
    cache.clear()
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert admin_nav.delivery_issues_open(request) == "2"
    request.user = StaffUserFactory.create(is_staff=False)
    assert admin_nav.delivery_issues_open(request) is None
