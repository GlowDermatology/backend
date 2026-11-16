"""The customer's order view of a Yandex, BTS and pickup order (plan §3.4): ``delivery`` on the summary and
the detail, the Yandex ``shipment`` card, the BTS-only ``tracking_number``, provider-aware timeline subtitles
and the ``delivery_failed`` / ``shipment_returned`` entries. No courier phone ever reaches the app."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import services
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import AT, pay, place, ship
from glowderma.core import enums, timeutils
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import Shipment
from glowderma.delivery.tests.factories import CourierClaimFactory, DeliveryQuoteFactory

pytestmark = pytest.mark.django_db
_E = enums.OrderEventType
TRACKING = "https://tracking.fake-yandex.invalid/abc"
EMPTY_DELIVERY = {
    "method": None,
    "method_label": None,
    "stage": None,
    "stage_label": None,
    "eta_from": None,
    "eta_to": None,
    "courier_name": None,
    "courier_vehicle": None,
    "tracking_url": None,
}


def _yandex(customer: Customer, method: str = "standard") -> Order:
    """A delivery order whose shipment is Yandex's with its checkout quote (what checkout writes)."""
    order = place(customer)
    quote = DeliveryQuoteFactory.create(
        customer=customer,
        method=method,
        created_at=AT,
        used_at=AT,
        order=order,
        customer_fee=order.delivery_fee,
    )
    Shipment.objects.filter(order=order).update(provider="yandex", method=method, quote=quote)
    return order


def _detail(client: APIClient, order: Order, lang: str = "uz") -> dict[str, Any]:
    response = client.get(f"/api/v1/me/orders/{order.pk}/", HTTP_ACCEPT_LANGUAGE=lang)
    assert response.status_code == 200, response.json()
    return dict(response.json())


def _in_delivery(order: Order, *, ended: bool = False) -> Shipment:
    pay(order)
    shipment = Shipment.objects.get(order=order)
    claim = CourierClaimFactory.create(
        shipment=shipment,
        status="pickuped",
        accept_sent_at=AT,
        accepted_at=AT,
        courier_name="Akmal",
        courier_vehicle="Chevrolet Cobalt oq 01A777AA",
        tracking_url=TRACKING,
        ended_at=AT + timedelta(hours=2) if ended else None,
    )
    Shipment.objects.filter(pk=shipment.pk).update(
        state=Shipment.State.CREATED, claim=claim, stage=enums.ShipmentStage.IN_TRANSIT
    )
    shipment.refresh_from_db()
    return shipment


def test_yandex_order_before_payment_shows_the_quoted_window(
    customer_client: APIClient, customer: Customer
) -> None:
    order = _yandex(customer)
    quote = Shipment.objects.get(order=order).quote
    assert quote is not None and quote.eta_from is not None and quote.eta_to is not None
    body = _detail(customer_client, order)
    assert body["delivery"] == {
        **EMPTY_DELIVERY,
        "provider": "yandex",
        "method": "standard",
        "method_label": "Standart",
        "eta_from": timeutils.isoformat(quote.eta_from),
        "eta_to": timeutils.isoformat(quote.eta_to),
    }
    assert body["shipment"] is None and body["tracking_number"] is None
    listed = customer_client.get("/api/v1/me/orders/").json()["results"][0]
    assert listed["delivery"] == body["delivery"] and listed["tracking_number"] is None


def test_yandex_order_in_delivery_shows_the_courier_and_tracking(
    customer_client: APIClient, customer: Customer
) -> None:
    order = _yandex(customer, method="express")
    _in_delivery(order)
    body = _detail(customer_client, order, lang="ru")
    delivery = body["delivery"]
    assert (delivery["provider"], delivery["method"], delivery["method_label"]) == (
        "yandex",
        "express",
        "Экспресс",
    )
    assert (delivery["stage"], delivery["stage_label"]) == ("in_transit", "В пути")
    assert (delivery["courier_name"], delivery["courier_vehicle"]) == (
        "Akmal",
        "Chevrolet Cobalt oq 01A777AA",
    )
    assert delivery["tracking_url"] == TRACKING
    assert body["shipment"] == {"carrier": "Yandex", "tracking_number": "", "tracking_url": TRACKING}
    assert body["tracking_number"] is None
    assert "+998" not in json.dumps(body["delivery"]) + json.dumps(body["shipment"])  # no courier phone


def test_a_finished_claim_hides_the_courier(customer_client: APIClient, customer: Customer) -> None:
    order = _yandex(customer)
    _in_delivery(order, ended=True)
    body = _detail(customer_client, order)
    assert body["delivery"]["courier_name"] is None and body["delivery"]["tracking_url"] is None
    assert body["shipment"] == {"carrier": "Yandex", "tracking_number": "", "tracking_url": None}


def test_yandex_timeline_has_no_bts_wording_and_the_new_entries(
    customer_client: APIClient, customer: Customer
) -> None:
    order = _yandex(customer)
    shipment = _in_delivery(order)
    claim_id = shipment.claim.claim_id if shipment.claim is not None else ""
    with pinned_clock(AT + timedelta(minutes=10)):
        services.record_event(
            order, _E.SHIPMENT_CREATED, {"tracking_number": claim_id, "tariff": 20_000, "manual": False}
        )
    with pinned_clock(AT + timedelta(minutes=20)):
        services.record_event(
            order,
            _E.HANDED_TO_COURIER,
            {"via": "poll", "bts_status": "pickuped", "push_sent": False},
            source=enums.EventSource.YANDEX,
        )
    with pinned_clock(AT + timedelta(minutes=40)):
        services.record_event(
            order,
            _E.DELIVERY_FAILED,
            {
                "provider": "yandex",
                "carrier_status": "returning",
                "reason": "recipient_unreachable",
                "claim_id": claim_id,
            },
        )
    with pinned_clock(AT + timedelta(minutes=90)):
        services.record_event(
            order,
            _E.SHIPMENT_RETURNED,
            {"provider": "yandex", "carrier_status": "returned_finish", "claim_id": claim_id},
        )
    timeline = _detail(customer_client, order)["timeline"]
    types = [entry["type"] for entry in timeline]
    assert types[:4] == ["shipment_returned", "delivery_failed", "handed_to_courier", "shipment_created"]
    by_type = {entry["type"]: entry for entry in timeline}
    assert by_type["shipment_created"]["subtitle"] == "Yandex Dostavka"  # never the claim id
    assert by_type["handed_to_courier"]["subtitle"] == "Yandex Dostavka"
    assert by_type["delivery_failed"]["title"] == "Yetkazib bo‘lmadi"
    assert (
        by_type["delivery_failed"]["subtitle"] == "Buyurtma do‘konga qaytariladi, biz siz bilan bog‘lanamiz"
    )
    assert by_type["shipment_returned"]["title"] == "Buyurtma do‘konga qaytdi"
    assert all("BTS" not in (entry["subtitle"] or "") for entry in timeline)
    assert all(claim_id not in (entry["subtitle"] or "") for entry in timeline)
    english = {e["type"]: e for e in _detail(customer_client, order, lang="en")["timeline"]}
    assert english["delivery_failed"]["title"] == "Delivery failed"


def test_bts_order_keeps_its_shipment_card_and_tracking(
    customer_client: APIClient, customer: Customer
) -> None:
    order = ship(place(customer))
    body = _detail(customer_client, order)
    assert body["delivery"] == {**EMPTY_DELIVERY, "provider": "bts"}
    tracking = Shipment.objects.get(order=order).tracking_number
    assert tracking and body["tracking_number"] == tracking
    assert body["shipment"] == {"carrier": "BTS", "tracking_number": tracking, "tracking_url": None}
    created = next(entry for entry in body["timeline"] if entry["type"] == "shipment_created")
    assert created["subtitle"] == f"BTS kuzatuv raqami: {tracking}"


def test_pickup_order_has_no_delivery(customer_client: APIClient, customer: Customer) -> None:
    order = place(customer, fulfilment="pickup")
    body = _detail(customer_client, order)
    assert body["delivery"] is None and body["shipment"] is None
