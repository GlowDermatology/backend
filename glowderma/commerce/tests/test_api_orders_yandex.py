"""The admin orders API with Yandex Delivery (plan §3.10): the additive ``delivery`` object of the summary and
the detail (Yandex / BTS / pickup), the ``provider`` / ``stage`` / ``attention`` filters of ``orders/`` and
``orders/status-counts/``, and fixed query budgets."""

from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.commerce import delivery_admin
from glowderma.commerce.models import Order
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.commerce.tests.test_query_counts import DETAIL_QUERIES
from glowderma.core import enums, timeutils
from glowderma.delivery.models import CourierClaim, Shipment
from glowderma.delivery.tests.factories import (
    DeliveryIssueFactory,
    DeliveryQuoteFactory,
    ShipmentFactory,
    yandex_shipment,
)

pytestmark = pytest.mark.django_db
ORDERS = "/api/v1/admin/orders/"
COUNTS = "/api/v1/admin/orders/status-counts/"
SUMMARY_KEYS = {"id", "customer", "created_at", "total", "status", "payment", "fulfilment", "shipment"}
DELIVERY_KEYS = {"provider", *delivery_admin.YANDEX_KEYS, "open_issues"}


def _yandex_order(stage: str = "courier_assigned", **order: Any) -> Shipment:
    placed = OrderFactory.create(paid=True, **order)
    quote = DeliveryQuoteFactory.create(customer=placed.customer, carrier_price=20_000, customer_fee=22_500)
    now = timeutils.now()
    shipment = yandex_shipment(
        state="created",
        stage=stage,
        order=placed,
        quote=quote,
        parcel_weight_g=900,
        prepared_at=now,
        promised_from=now + timedelta(minutes=30),
        promised_to=now + timedelta(minutes=150),
        cost_difference=-2_500,
    )
    CourierClaim.objects.filter(pk=cast(int, shipment.claim_id)).update(
        courier_name="Fake Kuryer",
        courier_vehicle="Chevrolet Cobalt oq 01A777AA",
        tracking_url="https://tracking.fake-yandex.invalid/abc",
        approved_price=21_000,
    )
    shipment.refresh_from_db()
    return shipment


def _ids(response: Any) -> list[int]:
    assert response.status_code == 200, response.json()
    return sorted(row["id"] for row in response.json()["results"])


# --- the delivery object ------------------------------------------------------------------------------------


def test_a_yandex_order_shows_its_carrier_view(staff_client: APIClient) -> None:
    shipment = _yandex_order()
    issue = DeliveryIssueFactory.create(order=shipment.order, urgent=True, code="performer_not_found")
    DeliveryIssueFactory.create(
        order=shipment.order, kind="incident", state="resolved", resolved_at=timeutils.now()
    )  # closed: not listed
    detail = staff_client.get(f"{ORDERS}{shipment.order_id}/").json()
    delivery = detail["delivery"]
    assert set(delivery) == DELIVERY_KEYS
    claim = CourierClaim.objects.get(pk=cast(int, shipment.claim_id))
    assert delivery | {
        "promised_from": None,
        "promised_to": None,
        "prepared_at": None,
        "open_issues": [],
    } == {
        "provider": "yandex",
        "method": "standard",
        "stage": "courier_assigned",
        "promised_from": None,
        "promised_to": None,
        "quote_fee": 22_500,
        "carrier_price": 20_000,
        "offer_price": 20_000,
        "approved_price": 21_000,
        "carrier_cost_total": 0,
        "cost_difference": -2_500,
        "parcel_weight_g": 900,
        "prepared_at": None,
        "courier_name": "Fake Kuryer",
        "courier_vehicle": "Chevrolet Cobalt oq 01A777AA",
        "tracking_url": "https://tracking.fake-yandex.invalid/abc",
        "claim_status": "accepted",
        "claim_number": claim.number,
        "failure_code": None,
        "open_issues": [],
    }
    assert delivery["promised_from"] and delivery["promised_to"] and delivery["prepared_at"]
    assert delivery["open_issues"] == [
        {
            "id": issue.pk,
            "kind": "dispatch_failed",
            "code": "performer_not_found",
            "urgent": True,
            "opened_at": delivery["open_issues"][0]["opened_at"],
        }
    ]
    assert "phone" not in str(delivery)
    # the existing keys are untouched (superset): the shipment union and the actions
    assert detail["shipment"]["state"] == "created"
    assert set(detail["available_actions"]) == {"transitions", "cancel", "retry_shipment"}
    row = next(item for item in staff_client.get(ORDERS).json()["results"] if item["id"] == shipment.order_id)
    assert SUMMARY_KEYS <= set(row) and row["delivery"] == delivery


def test_a_failed_claim_reports_its_code(staff_client: APIClient) -> None:
    shipment = _yandex_order(stage="failed")
    CourierClaim.objects.filter(pk=cast(int, shipment.claim_id)).update(
        error_code="estimating_failed", status="failed"
    )
    delivery = staff_client.get(f"{ORDERS}{shipment.order_id}/").json()["delivery"]
    assert delivery["failure_code"] == "estimating_failed" and delivery["claim_status"] == "failed"


def test_a_yandex_order_before_any_claim(staff_client: APIClient) -> None:
    order = OrderFactory.create(paid=True)
    yandex_shipment(order=order, stage="awaiting_dispatch", method="express")
    delivery = staff_client.get(f"{ORDERS}{order.pk}/").json()["delivery"]
    assert (delivery["provider"], delivery["method"], delivery["stage"]) == (
        "yandex",
        "express",
        "awaiting_dispatch",
    )
    assert (
        delivery["claim_status"] is None and delivery["quote_fee"] is None and delivery["offer_price"] is None
    )


def test_bts_and_pickup_orders(staff_client: APIClient) -> None:
    bts = OrderFactory.create(paid=True)
    ShipmentFactory.create(order=bts)
    incident = DeliveryIssueFactory.create(order=bts, kind="incident", code="staff_report")
    pickup = OrderFactory.create(paid=True, pickup=True)
    detail = staff_client.get(f"{ORDERS}{bts.pk}/").json()
    assert detail["delivery"]["provider"] == "bts"
    assert all(detail["delivery"][key] is None for key in delivery_admin.YANDEX_KEYS)
    assert [row["id"] for row in detail["delivery"]["open_issues"]] == [incident.pk]
    assert staff_client.get(f"{ORDERS}{pickup.pk}/").json()["delivery"] is None
    rows = {row["id"]: row for row in staff_client.get(ORDERS).json()["results"]}
    assert rows[pickup.pk]["delivery"] is None and rows[bts.pk]["delivery"] == detail["delivery"]


# --- filters ------------------------------------------------------------------------------------------------


def test_provider_stage_and_attention_filters(staff_client: APIClient) -> None:
    assigned = _yandex_order().order_id
    waiting_order = OrderFactory.create(paid=True)
    yandex_shipment(order=waiting_order, stage="awaiting_dispatch")
    bts = OrderFactory.create(paid=True)
    ShipmentFactory.create(order=bts)
    pickup = OrderFactory.create(paid=True, pickup=True)
    DeliveryIssueFactory.create(order=waiting_order)
    DeliveryIssueFactory.create(order=bts, kind="incident", urgent=True)
    DeliveryIssueFactory.create(order=pickup, kind="incident", state="resolved", resolved_at=timeutils.now())
    assert _ids(staff_client.get(ORDERS, {"provider": "yandex"})) == sorted([assigned, waiting_order.pk])
    assert _ids(staff_client.get(ORDERS, {"provider": "bts"})) == [bts.pk]
    assert _ids(staff_client.get(ORDERS, {"stage": "awaiting_dispatch"})) == [waiting_order.pk]
    assert _ids(staff_client.get(ORDERS, {"attention": "open"})) == sorted([waiting_order.pk, bts.pk])
    assert _ids(staff_client.get(ORDERS, {"attention": "urgent"})) == [bts.pk]
    assert _ids(staff_client.get(ORDERS, {"provider": "yandex", "attention": "open"})) == [waiting_order.pk]
    counts = staff_client.get(COUNTS, {"provider": "yandex"}).json()
    assert counts["all"] == 2 and counts["processing"] == 2
    assert staff_client.get(COUNTS, {"attention": "urgent"}).json()["all"] == 1
    assert staff_client.get(COUNTS, {"stage": "courier_assigned"}).json()["processing"] == 1
    for params in ({"provider": "dhl"}, {"stage": "lost"}, {"attention": "maybe"}):
        response = staff_client.get(ORDERS, params)
        assert response.status_code == 400 and response.json()["errors"][0]["code"] == "invalid_choice"
        assert staff_client.get(COUNTS, params).status_code == 400
    assert Order.objects.count() == 4


# --- query budgets ------------------------------------------------------------------------------------------


def _count(client: APIClient, url: str, params: dict[str, Any] | None = None) -> int:
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url, params or {})
    assert response.status_code == 200
    return len(queries)


def _mixed() -> None:
    _yandex_order()
    order = OrderFactory.create(paid=True)
    yandex_shipment(order=order, stage="awaiting_dispatch")
    DeliveryIssueFactory.create(order=order)
    bts = OrderFactory.create(paid=True)
    ShipmentFactory.create(order=bts)
    OrderFactory.create(paid=True, pickup=True)


def test_the_list_query_count_does_not_grow_with_yandex_rows(staff_client: APIClient) -> None:
    _mixed()
    staff_client.get(ORDERS)  # warm the auth session
    first = _count(staff_client, ORDERS, {"page_size": 50})
    _mixed()
    assert _count(staff_client, ORDERS, {"page_size": 50}) == first
    assert _count(staff_client, ORDERS, {"page_size": 50, "attention": "open"}) == first


def test_the_detail_query_budget(staff_client: APIClient, django_assert_num_queries: Any) -> None:
    shipment = _yandex_order()
    DeliveryIssueFactory.create(order=shipment.order)
    pickup = OrderFactory.create(paid=True, pickup=True)
    staff_client.get(ORDERS)
    # a Yandex order: + the shipment with its claim and quote, + the open-issue check of available_actions
    with django_assert_num_queries(DETAIL_QUERIES + 2):
        assert staff_client.get(f"{ORDERS}{shipment.order_id}/").status_code == 200
    # pickup reads nothing for its (null) delivery object
    with django_assert_num_queries(DETAIL_QUERIES - 1):
        assert staff_client.get(f"{ORDERS}{pickup.pk}/").status_code == 200
    assert enums.DeliveryProvider.YANDEX == shipment.provider
