"""The Billz ERROR events of ``commerce`` raise their alerts (core.alerts; docs/ops/monitoring.md):
``billz.sale_dead``, ``billz.refund_by_hand`` and ``billz.sale_of_cancelled_order``; and the sale push
declares its deferral window for the consistency report (K6)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings

from glowderma.commerce import services, tasks, topics
from glowderma.commerce.tests.support import admin
from glowderma.commerce.tests.test_billz_push import dispatch, message_of, paid_order
from glowderma.core import alerts, outbox
from glowderma.core.models import OutboxMessage
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
