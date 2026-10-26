"""Query budgets of the list/detail/export endpoints: the counts do not grow with the rows (no N+1 through
customer, shipment → create_message, payments, refunds, purchases)."""

from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.commerce import services, topics
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place, ship
from glowderma.core.seeding import pinned_clock

pytestmark = pytest.mark.django_db
ORDERS = "/api/v1/admin/orders/"
PAYMENTS = "/api/v1/admin/payments/"
DETAIL_QUERIES = 8  # the staff user + the detail reads; the same for every order shape


def _variety(staff_id: int) -> Order:
    """One order of each shape the list renders: awaiting/not_created, pickup paid, shipped (created), failed
    BTS create, Click cancel with a succeeded refund (payment refunded), Payme cancel (manual refund)."""
    place()
    pay(place(fulfilment="pickup"))
    ship(place())
    failed = pay(place(fake={"bts": {"fail_attempts": 1}}))
    deliver(topics.BTS_CREATE_SHIPMENT, failed.pk)
    refunded = ship(place())
    with pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(refunded.pk, "Mijoz so‘rovi bilan", staff_id)
        deliver(topics.REFUND_SUBMIT, refunded.pk)
    manual = pay(place(fulfilment="pickup", provider="payme"))
    with pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(manual.pk, "Mijoz so‘rovi bilan", staff_id)
    return refunded


def _count(client: APIClient, url: str, params: dict[str, Any]) -> int:
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, params)
    assert response.status_code == 200
    return len(queries)


def _warm(client: APIClient) -> None:
    """The first authenticated request also loads the auth session (cached afterwards)."""
    assert client.get(ORDERS, {"page_size": 6}).status_code == 200


def test_orders_list_and_export_counts_do_not_grow_with_the_rows(staff_client: APIClient) -> None:
    staff = admin()
    _variety(staff.pk)
    _warm(staff_client)
    small_page = _count(staff_client, ORDERS, {"page_size": 6})
    full_page = _count(staff_client, ORDERS, {"page_size": 50})
    export = _count(staff_client, ORDERS, {"export": "csv"})
    _variety(staff.pk)  # twice the rows of every shape
    assert Order.objects.count() == 12
    assert small_page == full_page == _count(staff_client, ORDERS, {"page_size": 50})
    assert export == _count(staff_client, ORDERS, {"export": "csv"})


def test_order_detail_has_a_fixed_query_budget(
    staff_client: APIClient, django_assert_num_queries: Any
) -> None:
    staff = admin()
    refunded = _variety(staff.pk)
    _warm(staff_client)
    with django_assert_num_queries(DETAIL_QUERIES):
        response = staff_client.get(f"{ORDERS}{refunded.pk}/")
    assert response.status_code == 200
    body = response.json()
    assert body["refunds"] and body["shipment"]["state"] == "created" and body["history"]


def test_payments_list_and_export_counts_do_not_grow_with_the_rows(staff_client: APIClient) -> None:
    staff = admin()
    _variety(staff.pk)
    _warm(staff_client)
    page_20 = _count(staff_client, PAYMENTS, {"page_size": 20})
    page_50 = _count(staff_client, PAYMENTS, {"page_size": 50})
    export = _count(staff_client, PAYMENTS, {"export": "csv"})
    _variety(staff.pk)
    assert page_20 == page_50 == _count(staff_client, PAYMENTS, {"page_size": 50})
    assert export == _count(staff_client, PAYMENTS, {"export": "csv"})
