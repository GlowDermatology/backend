"""The BTS handlers over the **live** adapter (customer contract §2.13.3, §2.14.3): the clientId lookup
before every create, a created parcel's waybill number, polling with BTS's own status codes (``300`` is "in
the sending office", never "delivered") and the admin cancel — against docs.bts.uz's answers (no network)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.db import connection, transaction
from django.test import override_settings

from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.commerce.tests.webhook_support import real_now
from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage
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


def test_an_order_cancelled_while_the_create_was_in_flight_gets_its_parcel_cancelled(script: Script) -> None:
    """R3: the create runs outside the Order lock, so an admin cancel can land while BTS creates the parcel;
    the re-lock sees the cancelled order and cancels the parcel at BTS as an orphan (never a live parcel of a
    cancelled order)."""
    order = paid_delivery_order()
    script.on("GET", "/v1/order/list", listing())

    def create_while_cancelled(request: httpx.Request) -> httpx.Response:
        with pinned_clock(AT + timedelta(hours=1)):
            services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)  # sees no parcel yet
        return httpx.Response(200, json=order_document(order.pk))

    script.on("POST", "/v1/order/add", create_while_cancelled)
    script.on("GET", "/v1/order-cancel/index", reply("bts", "cancel_ok"))
    with override_settings(**LIVE_BTS):
        deliver("bts.create_shipment", order.pk)
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.tracking_number) == ("cancelled", "4293644")
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="created", payload__orphan=True).exists()
    assert len(script.to("GET", "/v1/order-cancel/index")) == 1
    assert not OrderEvent.objects.filter(order=order, type="shipment_created").exists()


def test_the_lookup_window_starts_the_day_before_the_shipment_row(script: Script) -> None:
    """A-S4 / A-S5: ``beginDate`` = the shipment row's creation (before its first create request) minus a
    day, ``endDate`` = a week ahead."""
    order = paid_delivery_order()
    shipment = Shipment.objects.get(order=order)
    script.on("GET", "/v1/order/list", listing())
    script.on("POST", "/v1/order/add", httpx.Response(200, json=order_document(order.pk)))
    with override_settings(**LIVE_BTS):
        deliver("bts.create_shipment", order.pk)
    (lookup,) = script.to("GET", "/v1/order/list")
    created = timeutils.local_date(shipment.created_at)
    assert lookup.url.params["beginDate"] == (created - timedelta(days=1)).isoformat()
    today = timeutils.local_date(timeutils.now())
    assert lookup.url.params["endDate"] == (today + timedelta(days=7)).isoformat()


@pytest.mark.parametrize("code", [1300, 1400])
def test_polling_alerts_once_on_a_parcel_bts_deleted_or_let_expire(
    script: Script, code: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A-S8: 1300 / 1400 are recorded only (no order change), so polling raises an ERROR for staff."""
    shipment = stale_parcel("4293644")
    script.on("GET", "/v1/order/track", track(code))
    with override_settings(**LIVE_BTS):
        tasks.run_bts_poll()
        Shipment.objects.filter(pk=shipment.pk).update(last_status_at=real_now() - timedelta(minutes=60))
        tasks.run_bts_poll()
    assert caplog.text.count("bts.parcel_gone") == 1 and str(code) in caplog.text
    assert Order.objects.get(pk=shipment.order_id).status == "processing"


@pytest.mark.django_db(transaction=True)
def test_no_bts_call_runs_under_the_order_row_lock(script: Script, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3: while the lookup is in flight the Order row is free (a payment webhook, an admin action or the
    customer's order view never wait for BTS); a second delivery of the same request waits for nothing."""
    from glowderma.core import tasks as core_tasks

    monkeypatch.setattr(core_tasks.dispatch_outbox, "delay", lambda *args, **kwargs: None)
    order = paid_delivery_order()
    message = OutboxMessage.objects.get(topic="bts.create_shipment", payload__order_id=order.pk)
    in_lookup, release = threading.Event(), threading.Event()

    def slow_listing(request: httpx.Request) -> httpx.Response:
        in_lookup.set()
        release.wait(10)
        return listing()

    script.on("GET", "/v1/order/list", slow_listing)
    script.on("POST", "/v1/order/add", httpx.Response(200, json=order_document(order.pk)))
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with override_settings(**LIVE_BTS):
                tasks.handle_bts_create_shipment(message)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert in_lookup.wait(10)
        with transaction.atomic():  # nowait: raises at once if the worker held the row
            Order.objects.select_for_update(nowait=True).get(pk=order.pk)
        with pytest.raises(tasks.CreateBusy):  # an overlapping delivery never calls BTS concurrently
            tasks.create_shipment(message.payload)
    finally:
        release.set()
        thread.join(10)
    assert errors == []
    assert Shipment.objects.get(order=order).state == "created"
    assert len(script.to("POST", "/v1/order/add")) == 1
