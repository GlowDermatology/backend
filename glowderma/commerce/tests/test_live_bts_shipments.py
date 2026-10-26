"""The BTS handlers over the **live** adapter (customer contract §2.13.3, §2.14.3): the clientId lookup
before every create, a created parcel's waybill number, polling with BTS's own status codes (``300`` is "in
the sending office", never "delivered") and the admin cancel — against docs.bts.uz's answers (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.test import override_settings

from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.commerce.tests.webhook_support import real_now
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.integrations import httpclient
from glowderma.integrations.tests.live_fixtures import Script, load, read_timeout, reply

pytestmark = pytest.mark.django_db
LIVE_BTS = {
    "BTS_MODE": "live",
    "BTS_BASE_URL": "https://apitest.bts.uz:28345",
    "BTS_LOGIN": "glowderma",
    "BTS_PASSWORD": "bts-password-value",
    "BTS_SENDER_NAME": "Glow Derma",
    "BTS_SENDER_PHONE": "+998712000000",
    "BTS_SENDER_ADDRESS": "Amir Temur 1",
    "BTS_SENDER_CITY_CODE": "0101",
}


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[Script]:
    scripted = Script().on("POST", "/auth/login", reply("bts", "login_ok"))
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(scripted.transport))
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    yield scripted


def order_document(order_id: int, waybill: int = 4293644, code: int | None = None) -> dict[str, Any]:
    document: dict[str, Any] = load("bts", "create_ok")
    document["data"].update(orderId=waybill, clientId=str(order_id), status={"code": code, "info": "x"})
    return document


def listing(*orders: dict[str, Any]) -> httpx.Response:
    items = [order["data"] for order in orders]
    meta = {"totalCount": len(items), "pageCount": 1, "currentPage": 1, "perPage": 50}
    return httpx.Response(200, json={"status": True, "data": {"items": items, "_meta": meta}})


def paid_delivery_order() -> Order:
    order = place()  # created and paid while BTS is fake (the checkout quote is not under test here)
    pay(order)
    return order


def test_a_create_after_an_empty_lookup_stores_the_waybill_number(script: Script) -> None:
    order = paid_delivery_order()
    script.on("GET", "/v1/order/list", listing())
    script.on("POST", "/v1/order/add", httpx.Response(200, json=order_document(order.pk)))
    with override_settings(**LIVE_BTS):
        deliver("bts.create_shipment", order.pk)
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.tracking_number, shipment.bts_order_id) == (
        "created",
        "4293644",
        "4293644",
    )
    assert shipment.bts_status == "100" and shipment.tariff == order.delivery_fee  # BTS's cost is not read
    sent = script.to("POST", "/v1/order/add")[0]
    assert b'"clientId":"%d"' % order.pk in sent.content.replace(b" ", b"")
    assert [r.url.path for r in script.requests] == ["/auth/login", "/v1/order/list", "/v1/order/add"]
    assert OrderEvent.objects.filter(order=order, type="shipment_created").exists()


def test_a_parcel_bts_already_holds_is_adopted_never_duplicated(script: Script) -> None:
    order = paid_delivery_order()
    script.on("GET", "/v1/order/list", listing(order_document(order.pk, waybill=4300001)))
    with override_settings(**LIVE_BTS):
        deliver("bts.create_shipment", order.pk)
    assert Shipment.objects.get(order=order).tracking_number == "4300001"
    assert script.to("POST", "/v1/order/add") == []


def test_a_lookup_that_fails_creates_nothing(script: Script) -> None:
    order = paid_delivery_order()
    script.on("GET", "/v1/order/list", read_timeout)
    with override_settings(**LIVE_BTS), pytest.raises(Exception, match="BTS"):
        deliver("bts.create_shipment", order.pk)  # the outbox retries the message
    assert script.to("POST", "/v1/order/add") == []
    assert Shipment.objects.get(order=order).state == "not_created"


def test_a_refused_create_fails_the_shipment_for_staff(script: Script) -> None:
    order = paid_delivery_order()
    script.on("GET", "/v1/order/list", listing())
    script.on("POST", "/v1/order/add", reply("bts", "create_422", 422))
    with override_settings(**LIVE_BTS):
        deliver("bts.create_shipment", order.pk)
    shipment = Shipment.objects.get(order=order)
    assert shipment.state == "failed"
    event = OrderEvent.objects.get(order=order, type="shipment_failed")
    assert event.data["error_code"] == "validation_error"


def stale_parcel(waybill: str) -> Shipment:
    order = OrderFactory.create(paid=True)
    old = real_now() - timedelta(minutes=60)
    shipment = ShipmentFactory.create(
        order=order,
        created=True,
        tracking_number=waybill,
        bts_order_id=waybill,
        bts_status="100",
        bts_created_at=old,
        last_status_at=old,
    )
    ShipmentEvent.objects.create(
        shipment=shipment, kind="status_received", bts_status="100", via="polling", at=old, payload={}
    )
    return shipment


def track(code: int) -> httpx.Response:
    return httpx.Response(200, json={"status": True, "data": {"orderId": 4293644, "status": {"code": code}}})


def test_polling_reads_bts_codes_through_the_translation(script: Script) -> None:
    shipment = stale_parcel("4293644")
    script.on("GET", "/v1/order/track", track(300))
    with override_settings(**LIVE_BTS):
        assert tasks.run_bts_poll().tracked == [shipment.order_id]
    order = Order.objects.get(pk=shipment.order_id)
    assert order.status == "handed_to_courier"  # BTS 300 = in the sending office, never "delivered"
    (request,) = script.to("GET", "/v1/order/track")
    assert request.url.params["orderId"] == "4293644"
    script.on("GET", "/v1/order/track", track(1200))
    Shipment.objects.filter(pk=shipment.pk).update(last_status_at=real_now() - timedelta(minutes=60))
    with override_settings(**LIVE_BTS):
        tasks.run_bts_poll()
    order.refresh_from_db()
    assert order.status == "delivered"


def test_an_admin_cancel_of_a_parcel_bts_already_cancelled_is_idempotent(script: Script) -> None:
    order = paid_delivery_order()
    script.on("GET", "/v1/order/list", listing())
    script.on("POST", "/v1/order/add", httpx.Response(200, json=order_document(order.pk)))
    with override_settings(**LIVE_BTS):
        deliver("bts.create_shipment", order.pk)
        script.on("GET", "/v1/order-cancel/index", reply("bts", "cancel_already", 400))
        with pinned_clock(AT + timedelta(hours=1)):
            services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    order.refresh_from_db()
    shipment = Shipment.objects.get(order=order)
    assert order.status == "cancelled" and shipment.state == "cancelled"
    assert len(script.to("GET", "/v1/order-cancel/index")) == 1
