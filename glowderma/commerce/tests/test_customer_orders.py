"""``me/orders/`` and ``me/orders/{id}/`` (customer contract §2.8, §4): scopes, ownership, the history
horizon,
the №10248 timeline vector in uz/ru/en, progress, refunds, the display rule."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import selectors, services
from glowderma.commerce.models import Order
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.commerce.tests.support import admin, deliver, place, product, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import at, complete, prepare, real_now
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db
LIST = "/api/v1/me/orders/"
T = datetime(2026, 10, 23, 14, 2, tzinfo=TASHKENT)


@pytest.fixture
def now() -> datetime:
    seed_policies()
    return real_now()


def _vector_order(customer: Customer) -> Order:
    """Order №10248 of the admin §4 test vector, built from its events."""
    order = OrderFactory.create(
        customer=customer,
        handed=True,
        created_at=T,
        items_subtotal=808_000,
        promo_subtotal=240_000,
        delivery_fee=25_000,
        cashback_used=48_600,
    )
    ShipmentFactory.create(order=order, created=True, tracking_number="48213907")
    rec = services.record_event
    rec(order, "order_created", {"channel": "ios", "item_count": 3, "total": 784_400}, at=T)
    rec(
        order,
        "reservation_created",
        {"item_count": 3, "minutes": 15, "expires_at": "2026-10-23T14:17:00+05:00"},
        at=T,
    )
    paid_at = T + timedelta(minutes=3)
    rec(
        order,
        "payment_confirmed",
        {
            "provider": "click",
            "transaction_id": "CLK-7741920385",
            "amount": 784_400,
            "reservation_converted": True,
        },
        at=paid_at,
    )
    rec(
        order,
        "cashback_credited",
        {"ledger_entry_id": 121, "amount": 5680, "base": 568_000, "percent": "1.00", "policy_version": 3},
        at=paid_at,
    )
    rec(order, "billz_sale_recorded", {"billz_order_id": "0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10"}, at=paid_at)
    rec(
        order,
        "shipment_created",
        {"tracking_number": "48213907", "tariff": 25_000, "manual": False},
        at=T + timedelta(minutes=4),
    )
    rec(
        order,
        "handed_to_courier",
        {"via": "webhook", "bts_status": "200", "push_sent": True},
        source="bts",
        at=T + timedelta(hours=2, minutes=38),
    )
    return order


def test_the_10248_timeline_vector(customer_client: APIClient, customer: Customer) -> None:
    order = _vector_order(customer)
    Customer.objects.filter(pk=customer.pk).update(history_from=None)
    url = f"{LIST}{order.pk}/"
    uz = customer_client.get(url).json()["timeline"]
    assert [(e["type"], e["title"], e["subtitle"]) for e in uz] == [
        ("handed_to_courier", "Kuryerga topshirildi", "BTS"),
        ("shipment_created", "Yetkazib berishga topshirildi", "BTS kuzatuv raqami: 48213907"),
        ("payment_confirmed", "To‘lov qabul qilindi", "Click orqali · cashback +5 680 so‘m"),
        ("order_created", "Buyurtma berildi", "3 ta mahsulot · 784 400 so‘m"),
    ]
    ru = customer_client.get(url, HTTP_ACCEPT_LANGUAGE="ru-RU").json()["timeline"]
    assert ru[2]["subtitle"] == "через Click · кешбэк +5 680 сум" and ru[0]["title"] == "Передан курьеру"
    en = customer_client.get(url, HTTP_ACCEPT_LANGUAGE="en").json()["timeline"]
    assert en[3]["subtitle"] == "3 item(s) · 784,400 so'm"
    detail = customer_client.get(url).json()
    assert (
        detail["status_label"] == "Kuryerga topshirildi" and detail["fulfilment_label"] == "Yetkazib berish"
    )
    assert detail["shipment"] == {"carrier": "BTS", "tracking_number": "48213907", "tracking_url": None}
    assert [(p["step"], p["state"]) for p in detail["progress"]] == [
        ("placed", "done"),
        ("paid", "done"),
        ("with_courier", "done"),
        ("delivered", "current"),
    ]
    assert detail["actions"] == {"pay": False, "cancel": False, "reorder": True}


def test_list_scopes_ownership_and_horizon(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    awaiting = place(customer, fulfilment="pickup", when=now - timedelta(minutes=5))
    delivered = OrderFactory.create(customer=customer, delivered=True, created_at=now - timedelta(days=1))
    cancelled = OrderFactory.create(customer=customer, cancelled=True, created_at=now - timedelta(days=2))
    old = OrderFactory.create(customer=customer, delivered=True, created_at=now - timedelta(days=30))
    OrderFactory.create(customer=shopper(), delivered=True)
    Customer.objects.filter(pk=customer.pk).update(history_from=now - timedelta(days=10))

    def ids(scope: str | None = None) -> list[int]:
        params = {"scope": scope} if scope else {}
        return [row["id"] for row in customer_client.get(LIST, params).json()["results"]]

    assert ids() == [awaiting.pk, delivered.pk, cancelled.pk]
    assert (
        ids("active") == [awaiting.pk]
        and ids("completed") == [delivered.pk]
        and ids("cancelled") == [cancelled.pk]
    )
    assert customer_client.get(LIST, {"scope": "nope"}).status_code == 400
    assert customer_client.get(LIST, {"page_size": 50}).status_code == 400
    assert customer_client.get(f"{LIST}{old.pk}/").status_code == 404
    page = customer_client.get(LIST).json()
    assert page["page_size"] == 10 and page["count"] == 3
    summary = page["results"][0]
    assert summary["status"] == "awaiting_payment" and summary["pay_by"] is not None
    assert summary["payment"] == {
        "status": "pending",
        "status_label": "To‘lov kutilmoqda",
        "provider": "click",
        "in_progress": False,
    }


def test_pickup_delivered_and_cancelled_progress(customer_client: APIClient, customer: Customer) -> None:
    pickup = OrderFactory.create(
        customer=customer,
        delivered=True,
        fulfilment="pickup",
        address_text="",
        address_region_code="",
        address_city_code="",
        delivery_fee=0,
    )
    body = customer_client.get(f"{LIST}{pickup.pk}/").json()
    assert body["status_label"] == "Olib ketildi" and body["shipment"] is None and body["address"] is None
    assert [(p["step"], p["state"]) for p in body["progress"]] == [
        ("placed", "done"),
        ("paid", "done"),
        ("picked_up", "done"),
    ]
    cancelled = OrderFactory.create(customer=customer, cancelled=True)
    progress = customer_client.get(f"{LIST}{cancelled.pk}/").json()["progress"]
    assert [(p["step"], p["state"]) for p in progress] == [
        ("placed", "done"),
        ("paid", "skipped"),
        ("with_courier", "skipped"),
        ("delivered", "skipped"),
        ("cancelled", "done"),
    ]
    assert progress[-1]["label"] == "Bekor qilindi" and progress[-1]["at"] is not None


def test_refund_mapping_and_names(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    item = product(name_uz="Krem", name_ru="Крем", name_en="Cream")
    order = place(customer, [(item, 1)], fulfilment="pickup", when=now)
    services.mark_paid(
        order.pk, Payment.objects.get(order=order).pk, "click", "CLK-9", raw={"click_paydoc_id": "9"}
    )
    services.cancel_order(order.pk, "Mijoz so‘rovi", admin().pk)
    body = customer_client.get(f"{LIST}{order.pk}/", HTTP_ACCEPT_LANGUAGE="en").json()
    assert body["items"][0]["name"] == "Cream"
    assert body["refund"]["state"] == "in_progress" and body["refund"]["state_label"] == "Refund in progress"
    assert body["timeline"][0]["type"] == "cancelled"
    assert body["timeline"][0]["subtitle"] == "Cancelled by the shop · the payment will be refunded"
    assert body["cancellation"] == {"reason": "admin", "label": "Cancelled by the shop"}
    deliver("refund.submit", order.pk)
    body = customer_client.get(f"{LIST}{order.pk}/").json()
    assert body["refund"]["state"] == "completed" and body["refund"]["completed_at"] is not None
    assert body["payment"]["status"] == "refunded"
    assert body["timeline"][0]["type"] == "refund_succeeded"
    assert body["timeline"][0]["subtitle"].endswith("so‘m · Click")
    assert all(entry["type"] != "refund_in_progress" for entry in body["timeline"])  # folded into cancelled
    assert body["cashback"]["earned_state"] in ("reversed", "none")


def test_customer_inflight_order(customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=5)):
        prepare(order)
    with at(now + timedelta(seconds=30)):
        assert selectors.customer_inflight_order(customer.pk) == order.pk
    with at(now + timedelta(seconds=200)):
        assert selectors.customer_inflight_order(customer.pk) is None


# --- the display rule (customer contract §2.8)
# ----------------------------------------------------------------


def _views(client: APIClient, staff_client: APIClient, order: Order) -> dict[str, Any]:
    admin_list = staff_client.get("/api/v1/admin/orders/", {"search": str(order.pk)}).json()["results"]
    return {
        "admin_list": admin_list[0]["payment"],
        "admin_detail": staff_client.get(f"/api/v1/admin/orders/{order.pk}/").json()["payment"],
        "me_list": client.get(LIST).json()["results"][0]["payment"],
        "me_detail": client.get(f"{LIST}{order.pk}/").json(),
    }


def test_double_payment_shows_the_payme_payment(
    customer_client: APIClient, staff_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        payment_id = int(prepare(order)["merchant_prepare_id"])
    with at(now + timedelta(minutes=4)):
        payme_payment = services.ensure_payable_payment(order.pk, "payme")
        services.mark_paid(order.pk, payme_payment.id, "payme", "payme-paid")
        assert complete(order, payment_id)["error"] == 0
    views = _views(customer_client, staff_client, order)
    assert views["admin_list"]["provider"] == views["admin_detail"]["provider"] == "payme"
    assert views["me_list"]["provider"] == views["me_detail"]["payment"]["provider"] == "payme"
    assert views["me_detail"]["refund"] is None
    paid = staff_client.get("/api/v1/admin/orders/", {"payment": "paid", "search": str(order.pk)}).json()
    assert [row["id"] for row in paid["results"]] == [order.pk]
    assert any(e["type"] == "refund_in_progress" for e in views["me_detail"]["timeline"])


@pytest.mark.parametrize("arrangement", ["late_then_expired", "declined_then_late"])
def test_a_late_capture_next_to_a_failed_attempt_is_shown(
    customer_client: APIClient, staff_client: APIClient, customer: Customer, now: datetime, arrangement: str
) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    expires = order.reservation_expires_at
    assert expires is not None
    if arrangement == "late_then_expired":  # sandbox rows 11-12, then the order expires
        with at(now + timedelta(seconds=10)):
            first = int(prepare(order, 1001)["merchant_prepare_id"])
        with at(now + timedelta(seconds=20)):
            prepare(order, 1002)
            assert complete(order, first, 1001)["error"] == 0
        with at(expires + timedelta(seconds=400)):
            services.expire_due_reservations()
        late = first
    else:  # a declined first attempt, then a capture that lands after the grace
        with at(now + timedelta(seconds=10)):
            first = int(prepare(order, 1001)["merchant_prepare_id"])
            complete(order, first, 1001, error=-5017)
        with at(now + timedelta(seconds=20)):
            late = int(prepare(order, 1002)["merchant_prepare_id"])
        with at(expires + timedelta(seconds=400)):
            services.expire_due_reservations()
            assert complete(order, late, 1002)["error"] == 0
    assert Payment.objects.get(pk=late).error_code == "late_capture"
    views = _views(customer_client, staff_client, order)
    assert views["admin_list"]["status"] == views["admin_detail"]["status"] == "paid"
    assert views["me_detail"]["payment"]["status"] == "paid" and views["me_list"]["status"] == "paid"
    assert views["me_detail"]["refund"]["state"] == "in_progress"
    paid = staff_client.get("/api/v1/admin/orders/", {"payment": "paid", "search": str(order.pk)}).json()
    failed = staff_client.get("/api/v1/admin/orders/", {"payment": "failed", "search": str(order.pk)}).json()
    assert len(paid["results"]) == 1 and failed["results"] == []
    deliver("refund.submit", order.pk)
    assert customer_client.get(f"{LIST}{order.pk}/").json()["payment"]["status"] == "refunded"


def test_a_late_capture_next_to_a_newer_pending_attempt(
    customer_client: APIClient, staff_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        first = int(prepare(order, 1001)["merchant_prepare_id"])
    with at(now + timedelta(seconds=20)):
        prepare(order, 1002)
        complete(order, first, 1001)
    body = customer_client.get(f"{LIST}{order.pk}/").json()
    assert body["payment"]["status"] == "pending" and body["refund"] is None
    assert any(entry["type"] == "refund_in_progress" for entry in body["timeline"])
    assert staff_client.get(f"/api/v1/admin/orders/{order.pk}/").json()["payment"]["status"] == "pending"
    assert Refund.objects.get(payment_id=first).origin == "late_capture"
