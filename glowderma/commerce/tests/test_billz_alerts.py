"""The Billz ERROR events of ``commerce`` raise their alerts (core.alerts; docs/ops/monitoring.md):
``billz.sale_dead``, ``billz.refund_by_hand`` and ``billz.sale_of_cancelled_order``; and the sale push
declares its deferral window for the consistency report (K6)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings

from glowderma.catalog.models import Product
from glowderma.commerce import services, tasks, topics
from glowderma.commerce.tests.support import admin, pay, place, shopper
from glowderma.commerce.tests.test_billz_push import dispatch, message_of, paid_order
from glowderma.core import alerts, outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.integrations.tests.billz_support import SALE_ID, BillzServer, live
from glowderma.loyalty.models import Purchase

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alerts_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    sent: list[bytes] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs["content"])
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    with override_settings(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook"):
        yield sent


def raised(event: str) -> list[dict[str, Any]]:
    return [
        message.payload["fields"]
        for message in OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC).order_by("id")
        if message.payload["event"] == event
    ]


def test_a_dead_sale_alerts_with_its_ids() -> None:
    order = paid_order(fake={"billz": {"push": "rejected"}})
    message = dispatch(message_of(order.pk))
    assert message.status == "dead"
    purchase = Purchase.objects.get(order=order)
    assert raised("billz.sale_dead") == [
        {"order_id": order.pk, "purchase_id": purchase.pk, "message_id": message.pk}
    ]
    assert raised("outbox.dead") == [
        {"topic": topics.BILLZ_PUSH_SALE, "message_id": message.pk, "attempts": 1}
    ]


def test_cancelling_an_order_with_a_billz_sale_alerts_after_the_commit(
    django_capture_on_commit_callbacks: Any,
) -> None:
    order = paid_order()
    Purchase.objects.filter(order=order).update(billz_sale_id=SALE_ID, billz_pushed_at=order.paid_at)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
        assert raised("billz.refund_by_hand") == []  # nothing before the cancel commits
    for callback in callbacks:
        callback()
    purchase = Purchase.objects.get(order=order)
    assert raised("billz.refund_by_hand") == [
        {"order_id": order.pk, "purchase_id": purchase.pk, "pushed": True}
    ]


def test_a_push_stopped_by_a_cancelled_order_alerts() -> None:
    order = paid_order()
    services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    Purchase.objects.filter(order=order).update(billz_sale_id=SALE_ID)
    tasks.handle_billz_push_sale(message_of(order.pk))
    purchase = Purchase.objects.get(order=order)
    assert raised("billz.sale_of_cancelled_order") == [
        {"order_id": order.pk, "purchase_id": purchase.pk, "case": "push_stopped"}
    ]


@override_settings(BILLZ_MODE="live")
def test_an_order_cancelled_during_the_push_alerts(django_capture_on_commit_callbacks: Any) -> None:
    order = paid_order()
    server = BillzServer()
    staff = admin()
    original = server._default

    def recalc(request: httpx.Request) -> httpx.Response:
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
        return original("POST /v1/recalculate-order-bill/{order_id}", request)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = recalc
    with (
        patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)),
        django_capture_on_commit_callbacks(execute=True),
    ):
        dispatch(message_of(order.pk))
    purchase = Purchase.objects.get(order=order)
    assert raised("billz.sale_of_cancelled_order") == [
        {"order_id": order.pk, "purchase_id": purchase.pk, "case": "cancelled_during_push"}
    ]
    assert raised("billz.refund_by_hand") == [
        {"order_id": order.pk, "purchase_id": purchase.pk, "pushed": False}
    ]


@override_settings(BILLZ_MODE="live")
def test_a_sale_paid_after_the_cancel_alerts_once_linked(django_capture_on_commit_callbacks: Any) -> None:
    order = paid_order()
    server = BillzServer()
    staff = admin()
    original = server._default

    def payment(request: httpx.Request) -> httpx.Response:
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
        return original("POST /v2/order-payment/{order_id}", request)

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    with (
        patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)),
        django_capture_on_commit_callbacks(execute=True),
    ):
        dispatch(message_of(order.pk))
    purchase = Purchase.objects.get(order=order)
    assert {"order_id": order.pk, "purchase_id": purchase.pk, "case": "paid_after_cancel"} in raised(
        "billz.sale_of_cancelled_order"
    )


def test_the_sale_push_declares_its_deferral_window() -> None:
    with override_settings(BILLZ_SHIFT_RETRY_MAX_HOURS=12):
        assert outbox.deferral_windows()[topics.BILLZ_PUSH_SALE] == timedelta(hours=12)


def two_paid_orders(**kwargs: Any) -> list[Any]:
    """Two paid pickup orders of the same products by two customers (``paid_order`` creates its products)."""
    first = paid_order(**kwargs)
    serum, foam = (Product.objects.get(billz_id=billz_id) for billz_id in ("4607012301", "4607012302"))
    customer = shopper(phone="+998901234568")
    return [first, pay(place(customer, [(serum, 1), (foam, 1)], fulfilment="pickup", **kwargs))]


def test_dead_sales_of_two_orders_raise_two_alerts_in_one_window() -> None:
    first, second = two_paid_orders(fake={"billz": {"push": "rejected"}})
    dead = []
    for order in (first, second):
        # dispatch() parks every other message as done: the second order's push is put back first
        OutboxMessage.objects.filter(pk=message_of(order.pk).pk).update(status="pending")
        dead.append(dispatch(message_of(order.pk)))
    assert [message.status for message in dead] == ["dead", "dead"]
    assert [fields["order_id"] for fields in raised("billz.sale_dead")] == [first.pk, second.pk]
    assert len(raised("outbox.dead")) == 1  # the outbox.dead alert stays one per topic and window


def test_refunds_by_hand_of_two_orders_raise_two_alerts(django_capture_on_commit_callbacks: Any) -> None:
    orders = two_paid_orders()
    for order, sale_id in zip(orders, (SALE_ID, str(uuid.uuid4())), strict=True):
        Purchase.objects.filter(order=order).update(billz_sale_id=sale_id, billz_pushed_at=order.paid_at)
    with django_capture_on_commit_callbacks(execute=True):
        for order in orders:
            services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    assert [fields["order_id"] for fields in raised("billz.refund_by_hand")] == [order.pk for order in orders]


def test_cancelled_order_sales_alert_once_per_order_and_window() -> None:
    with pinned_clock(timeutils.now()):
        tasks._alert_cancelled_sale(101, 1, "push_stopped")
        tasks._alert_cancelled_sale(102, 2, "push_stopped")
        tasks._alert_cancelled_sale(101, 1, "paid_after_cancel")  # same order, same window: deduplicated
    assert [fields["order_id"] for fields in raised("billz.sale_of_cancelled_order")] == [101, 102]
    keys = OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC).values_list("dedupe_key", flat=True)
    assert sorted(key.split(":")[2] for key in keys) == ["101", "102"]
