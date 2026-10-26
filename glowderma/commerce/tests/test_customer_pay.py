"""``orders/{id}/pay/`` and ``orders/{id}/cancel/`` (customer contract §2.7.2, §2.7.3)."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import services
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import admin, place, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import at, ms, payme, prepare, real_now, ref, tiyin
from glowderma.core.models import OutboxMessage
from glowderma.integrations.payme import sandbox
from glowderma.payments.models import Payment

pytestmark = pytest.mark.django_db


def key() -> dict[str, str]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def pay(client: APIClient, order: Order, provider: str | None = None, **extra: Any) -> Any:
    return client.post(f"/api/v1/orders/{order.pk}/pay/", {"provider": provider}, format="json", **{**key(), **extra})


def cancel(client: APIClient, order: Order) -> Any:
    return client.post(f"/api/v1/orders/{order.pk}/cancel/", format="json", **key())


@pytest.fixture
def now() -> datetime:
    seed_policies()
    return real_now()


def test_pay_returns_the_click_page(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    payment = Payment.objects.get(order=order)
    with at(now + timedelta(seconds=10)):
        missing = customer_client.post(f"/api/v1/orders/{order.pk}/pay/", {}, format="json")
        body = pay(customer_client, order).json()
    assert missing.json()["code"] == "idempotency_key_required"
    assert body["order_id"] == order.pk and body["payment_id"] == payment.pk and body["provider"] == "click"
    assert body["amount"] == order.total and body["account_ref"] == ref(order) and body["app_url"] is None
    assert body["return_url"] == f"https://glowderma.uz/pay/return?order_id={order.pk}"
    url = urlsplit(body["web_url"])
    query = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://my.click.uz/services/pay"
    assert query["transaction_param"] == [ref(order)] and query["amount"] == [str(order.total)]
    assert query["return_url"] == [body["return_url"]]
    payment.refresh_from_db()
    assert payment.updated_at == Payment.objects.get(pk=payment.pk).updated_at
    assert Payment.objects.filter(order=order).count() == 1


def test_pay_payme_url_and_switch(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        body = pay(customer_client, order, "payme", HTTP_ACCEPT_LANGUAGE="ru").json()
    old = Payment.objects.filter(order=order).order_by("id").first()
    assert old is not None and (old.status, old.error_code) == ("failed", "abandoned")
    assert body["provider"] == "payme" and body["payment_id"] != old.pk
    encoded = body["web_url"].removeprefix("https://checkout.paycom.uz/")
    decoded = base64.b64decode(encoded).decode()
    assert f"ac.order_id={ref(order)}" in decoded and f"a={order.total * 100}" in decoded and "l=ru" in decoded
    assert "c=https%3A%2F%2Fglowderma.uz%2Fpay%2Freturn%3Forder_id%3D" in decoded
    order.refresh_from_db()
    assert order.payment_provider == "payme"
    assert not OrderEvent.objects.filter(order=order, type="payment_failed").exists()


def test_same_provider_reuses_the_row_while_click_is_open(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        assert prepare(order)["error"] == 0
    payment = Payment.objects.get(order=order)
    stamp = payment.updated_at
    with at(now + timedelta(seconds=20)):  # in flight
        assert pay(customer_client, order, "click").json()["payment_id"] == payment.pk
        detail = customer_client.get(f"/api/v1/me/orders/{order.pk}/").json()
    assert detail["actions"]["pay"] is True and detail["payment"]["in_progress"] is True
    assert detail["payment"]["in_progress_label"] == "To‘lov tekshirilmoqda" and detail["actions"]["cancel"] is False
    with at(now + timedelta(seconds=300)):  # open but no longer in flight
        assert pay(customer_client, order, "click").json()["payment_id"] == payment.pk
    payment.refresh_from_db()
    assert payment.updated_at == stamp


def test_switch_while_click_is_in_flight(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        assert prepare(order)["error"] == 0
    with at(now + timedelta(seconds=60)):
        refused = pay(customer_client, order, "payme")
    assert refused.status_code == 409 and refused.json()["code"] == "payment_in_progress"
    assert refused.json()["details"] == {"provider": "click", "order_id": order.pk}
    assert "Click" in refused.json()["message"]
    assert Payment.objects.filter(order=order).count() == 1
    with at(now + timedelta(seconds=131)):
        assert pay(customer_client, order, "payme").status_code == 200


def test_payme_state_1_blocks_a_switch_and_the_pay_action(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", provider="payme", when=now)
    with at(now + timedelta(seconds=10)):
        assert payme(sandbox.create("payme-1", ref(order), tiyin(order), ms(now)))["result"]["state"] == 1
        refused = pay(customer_client, order, "click")
        detail = customer_client.get(f"/api/v1/me/orders/{order.pk}/").json()
    assert refused.status_code == 409 and refused.json()["details"]["provider"] == "payme"
    assert detail["actions"]["pay"] is False and detail["actions"]["cancel"] is True


def test_not_payable_answers_order_expired_without_a_write(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires + timedelta(seconds=1)):
        response = pay(customer_client, order, "payme")
    assert response.status_code == 409 and response.json()["code"] == "order_expired"
    order.refresh_from_db()
    assert order.status == "awaiting_payment" and Payment.objects.filter(order=order).count() == 1


def test_already_paid_and_not_payable(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    services.mark_paid(order.pk, Payment.objects.get(order=order).pk, "click", "CLK-1", raw={"click_paydoc_id": "77"})
    assert pay(customer_client, order).json()["code"] == "already_paid"
    services.cancel_order(order.pk, "Mijoz so‘rovi", admin().pk)  # the refund is being made
    response = pay(customer_client, order)
    assert response.status_code == 409 and response.json()["code"] == "not_payable"
    other = place(shopper(), fulfilment="pickup", when=now)
    assert pay(customer_client, other).status_code == 404


def test_pay_refuses_a_provider_that_cannot_take_payments(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    from django.test import override_settings

    order = place(customer, fulfilment="pickup", when=now)
    with override_settings(DEV_FEATURES_ALLOWED=False, CLICK_MODE="fake", PAYME_MODE="fake"):
        response = pay(customer_client, order, "click")
    assert response.status_code == 400 and response.json()["errors"][0] == {
        "field": "provider",
        "code": "invalid_choice",
        "message": "Ruxsat etilmagan qiymat.",
    }


# --- customer cancel -----------------------------------------------------------------------------------------


def test_cancel_an_awaiting_click_order(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    response = cancel(customer_client, order)
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "cancelled" and detail["cancellation"] == {
        "reason": "customer_request",
        "label": "Siz bekor qildingiz",
    }
    assert detail["actions"] == {"pay": False, "cancel": False, "reorder": True}
    order.refresh_from_db()
    assert order.cancellation_reason == "customer_request" and order.cancelled_by_id is None
    payment = Payment.objects.get(order=order)
    assert (payment.status, payment.error_code) == ("failed", "cancelled")
    event = OrderEvent.objects.get(order=order, type="cancelled")
    assert event.source == "system" and event.actor_id is None
    assert event.data["reason"] == "customer_request" and event.data["stock_released"] is True
    assert OutboxMessage.objects.filter(topic="push.order_status", payload__status="cancelled").exists()
    assert detail["timeline"][0]["subtitle"] == "Siz bekor qildingiz"
    refused = cancel(customer_client, order)
    assert refused.status_code == 409 and refused.json()["code"] == "not_cancellable"
    assert refused.json()["message"] == "Bu buyurtmani ilovadan bekor qilib bo‘lmaydi. Sotuvchi bilan bog‘laning."


def test_cancel_leaves_a_payme_state_1_transaction_pending(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", provider="payme", when=now)
    with at(now + timedelta(seconds=10)):
        payme(sandbox.create("payme-1", ref(order), tiyin(order), ms(now)))
        assert cancel(customer_client, order).status_code == 200
    assert Payment.objects.get(order=order).status == "pending"


def test_cancel_while_click_is_in_flight(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = place(customer, fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=10)):
        prepare(order)
    with at(now + timedelta(seconds=30)):
        refused = cancel(customer_client, order)
    assert refused.status_code == 409 and refused.json()["code"] == "payment_in_progress"
    assert refused.json()["details"] == {"provider": "click", "order_id": order.pk}
    with at(now + timedelta(seconds=200)):
        assert cancel(customer_client, order).status_code == 200


def test_cancel_of_another_customers_order(customer_client: APIClient, now: datetime) -> None:
    other = place(shopper(), fulfilment="pickup", when=now)
    assert cancel(customer_client, other).status_code == 404
    assert services.selectors.customer_inflight_order(other.customer_id) is None
