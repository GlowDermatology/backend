"""Uzum Checkout end to end over the fake (docs/integrations/uzum.md): pay, the callback (a hint only), the
status sync, the state rule against Click / Payme / cancel / expiry, late captures, refunds, reconcile."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import selectors, services, tasks, uzum_flow
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import admin, place, seed_policies
from glowderma.commerce.tests.webhook_support import at, payme, prepare, real_now, ref, tiyin
from glowderma.core import enums, integration_modes, labels
from glowderma.core.models import OutboxMessage
from glowderma.integrations.models import IntegrationState, WebhookInbox
from glowderma.integrations.payme import sandbox as payme_sandbox
from glowderma.integrations.uzum import fake as fake_uzum
from glowderma.integrations.uzum import protocol
from glowderma.integrations.uzum import sandbox as uzum_sandbox
from glowderma.integrations.uzum.ports import OrderStatus
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db
CALLBACK = "/api/v1/webhooks/uzum/"
UZUM = enums.PaymentProvider.UZUM


@pytest.fixture
def now() -> datetime:
    seed_policies()
    return real_now()


def pay(client: APIClient, order: Order, **extra: Any) -> Any:
    return client.post(
        f"/api/v1/orders/{order.pk}/pay/",
        {"provider": "uzum"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        **extra,
    )


def uzum_order(customer: Customer, now: datetime, **kwargs: Any) -> Order:
    return place(customer, fulfilment="pickup", provider=UZUM, when=now, **kwargs)


def registered(client: APIClient, order: Order, when: datetime) -> Payment:
    with at(when):
        body = pay(client, order).json()
    payment = Payment.objects.get(pk=body["payment_id"])
    assert payment.provider_txn_id, body
    return payment


def callback(payment: Payment, *, token: str | None = None, **fields: Any) -> Any:
    body = uzum_sandbox.callback(
        order_id=fields.pop("order_id", payment.provider_txn_id),
        order_number=fields.pop("order_number", payment.provider_state["order_number"]),
        **fields,
    )
    secret = settings.UZUM_CALLBACK_TOKEN if token is None else token
    return APIClient().post(f"{CALLBACK}?token={secret}", body, format="json")


def sync_messages() -> int:
    count = 0
    for message in OutboxMessage.objects.filter(topic="uzum.sync_order", status="pending").order_by("id"):
        tasks.sync_uzum_order(message)
        OutboxMessage.objects.filter(pk=message.pk).update(status="done")
        count += 1
    return count


def run_refunds(order: Order) -> None:
    for message in OutboxMessage.objects.filter(
        topic="refund.submit", status="pending", payload__order_id=order.pk
    ).order_by("id"):
        tasks.handle_refund_submit(message)
        OutboxMessage.objects.filter(pk=message.pk).update(status="done")


# --- pay -------------------------------------------------------------------------------------------------


def test_pay_registers_one_form_and_hands_it_out_again(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    with at(now + timedelta(seconds=10)):
        first = pay(customer_client, order, HTTP_ACCEPT_LANGUAGE="ru").json()
    with at(now + timedelta(seconds=40)):
        second = pay(customer_client, order).json()
    assert first["provider"] == "uzum" and first["web_url"] == second["web_url"]
    assert first["payment_id"] == second["payment_id"] and first["app_url"] is None
    assert fake_uzum.calls == [("register", Payment.objects.get(pk=first["payment_id"]).provider_state["order_number"])]
    payment = Payment.objects.get(pk=first["payment_id"])
    assert protocol.payment_id_of(payment.provider_state["order_number"], "gd") == payment.pk
    status = fake_uzum.FakeUzum().order_status(payment.provider_txn_id)
    assert status.amount == tiyin(order)  # tiyin, never so'm
    pay_by = datetime.fromisoformat(first["pay_by"])
    assert pay_by <= datetime.fromisoformat(payment.provider_state["session_expires_at"])


def test_pay_when_uzum_is_down_stores_nothing_and_the_next_tap_registers_afresh(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now, fake={"uzum": {"register": "unknown"}})
    with at(now + timedelta(seconds=10)):
        answer = pay(customer_client, order)
    assert answer.status_code == 503 and answer.json()["code"] == "provider_unavailable"
    payment = Payment.objects.get(order=order)
    assert payment.provider_txn_id == "" and "registered_at" not in payment.provider_state
    Order.objects.filter(pk=order.pk).update(metadata={})
    with at(now + timedelta(seconds=20)):
        assert pay(customer_client, order).status_code == 200
    numbers = [ref for method, ref in fake_uzum.calls if method == "register"]
    assert len(numbers) == 2 and numbers[0] != numbers[1]  # the lost one may have taken its number


def test_no_form_is_registered_that_could_outlive_the_reservation_grace(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    late = order.reservation_expires_at - timedelta(seconds=60)
    with at(late):
        answer = pay(customer_client, order)
    assert answer.status_code == 409 and answer.json()["code"] == "order_expired"
    assert fake_uzum.calls == []


def test_a_spent_form_is_replaced_by_a_new_payment(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    first = registered(customer_client, order, now + timedelta(seconds=5))
    spent = datetime.fromisoformat(first.provider_state["session_expires_at"]) + timedelta(minutes=3)
    if spent + timedelta(seconds=720) > order.reservation_expires_at + timedelta(seconds=300):
        Order.objects.filter(pk=order.pk).update(reservation_expires_at=spent + timedelta(minutes=20))
    second = registered(customer_client, order, spent)
    first.refresh_from_db()
    assert (first.status, first.error_code) == ("failed", "abandoned") and second.pk != first.pk


# --- the callback is a hint ------------------------------------------------------------------------------


def test_a_callback_without_the_token_is_refused(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    payment = registered(customer_client, uzum_order(customer, now), now + timedelta(seconds=5))
    answer = callback(payment, token="wrong")
    assert answer.status_code == 401
    assert OutboxMessage.objects.filter(topic="uzum.sync_order").count() == 0
    (row,) = WebhookInbox.objects.filter(provider="uzum")
    assert row.event_key.startswith("rejected:") and "token" not in row.path


def test_a_forged_success_callback_pays_nothing(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    assert callback(payment).status_code == 200  # Uzum's status still says REGISTERED
    assert sync_messages() == 1
    payment.refresh_from_db()
    order.refresh_from_db()
    assert payment.status == "pending" and order.status == "awaiting_payment"


def test_a_callback_for_another_order_number_is_ignored(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    payment = registered(customer_client, uzum_order(customer, now), now + timedelta(seconds=5))
    assert callback(payment, order_number="stg-1-0a1b2c3d").json() == {"ok": True, "ignored": True}
    assert callback(payment, order_id=str(uuid.uuid4())).json() == {"ok": True, "ignored": True}
    assert OutboxMessage.objects.filter(topic="uzum.sync_order").count() == 0


def test_the_status_sync_pays_the_order_once(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    fake_uzum.complete(payment.provider_txn_id)
    assert callback(payment).status_code == 200
    assert callback(payment).status_code == 200  # Uzum's retry: one sync only
    assert sync_messages() == 1
    payment.refresh_from_db()
    order.refresh_from_db()
    assert payment.status == "paid" and order.status == "processing"
    assert OrderEvent.objects.filter(order=order, type="payment_confirmed").count() == 1
    assert uzum_flow.sync_payment(payment.pk) == "unchanged"


@pytest.mark.parametrize(
    "status",
    [
        {"completed_amount": 1},  # a capture of another amount
        {"merchant_order_id": "gd-1-00000000"},  # another of our numbers
        {"amount": 100},  # registered for another amount
    ],
)
def test_a_status_that_does_not_match_the_payment_is_never_honoured(
    customer_client: APIClient, customer: Customer, now: datetime, status: dict[str, Any]
) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    fields: dict[str, Any] = {
        "order_id": payment.provider_txn_id,
        "status": "COMPLETED",
        "merchant_order_id": payment.provider_state["order_number"],
        "amount": tiyin(order),
        "completed_amount": tiyin(order),
        "refunded_amount": 0,
        "reversed_amount": 0,
    }
    assert uzum_flow.apply_status(payment.pk, OrderStatus(**{**fields, **status})) == "mismatch"
    payment.refresh_from_db()
    assert payment.status == "pending"


def test_a_declined_payment_fails_and_the_order_stays_payable(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    fake_uzum.decline(payment.provider_txn_id)
    with at(now + timedelta(seconds=60)):
        assert uzum_flow.sync_payment(payment.pk) == "failed"
    payment.refresh_from_db()
    order.refresh_from_db()
    assert (payment.status, payment.error_code) == ("failed", "declined") and order.status == "awaiting_payment"
    assert OrderEvent.objects.filter(order=order, type="payment_failed").exists()


def test_a_form_nobody_paid_fails_once_its_session_is_over(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    with at(now + timedelta(minutes=5)):
        assert uzum_flow.sync_payment(payment.pk) == "unchanged"  # the form may still be paid
    with at(now + timedelta(minutes=15)):
        assert uzum_flow.sync_payment(payment.pk) == "failed"
    payment.refresh_from_db()
    assert payment.error_code == "session_expired"


# --- the state rule ----------------------------------------------------------------------------------------


def test_a_live_form_blocks_cancel_switches_and_other_providers(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    registered(customer_client, order, now + timedelta(seconds=5))
    with at(now + timedelta(seconds=30)):
        cancelled = customer_client.post(
            f"/api/v1/orders/{order.pk}/cancel/", format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        switched = customer_client.post(
            f"/api/v1/orders/{order.pk}/pay/",
            {"provider": "click"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        click = prepare(order)
        payme_answer = payme(payme_sandbox.check_perform(ref(order), tiyin(order)))
        assert selectors.customer_inflight_order(customer.pk) == (order.pk, "uzum")
        detail = customer_client.get(f"/api/v1/me/orders/{order.pk}/").json()
    assert cancelled.status_code == 409 and cancelled.json()["code"] == "payment_in_progress"
    assert switched.status_code == 409 and switched.json()["code"] == "payment_in_progress"
    assert click["error"] != 0
    assert "error" in payme_answer
    assert detail["actions"]["cancel"] is False


def test_expiry_never_fails_a_form_that_may_still_take_the_money(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    expires = order.reservation_expires_at
    assert not services.expire_reservation(order.pk, at=expires + timedelta(seconds=10))  # in grace
    assert services.expire_reservation(order.pk, at=expires + timedelta(minutes=6))
    payment.refresh_from_db()
    assert payment.status == "failed"  # the session ended inside the grace: failable


def test_admin_cancel_leaves_a_live_form_pending_and_a_late_capture_is_refunded(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    with at(now + timedelta(seconds=30)):
        services.cancel_order(order.pk, "mijoz so‘radi", admin().pk)
    payment.refresh_from_db()
    assert payment.status == "pending"  # the form may still be paid
    fake_uzum.complete(payment.provider_txn_id)
    with at(now + timedelta(seconds=90)):
        assert uzum_flow.sync_payment(payment.pk) == "late_capture"
    payment.refresh_from_db()
    assert payment.status == "paid" and payment.error_code == "late_capture"
    refund = Refund.objects.get(payment=payment)
    assert refund.state == "requested" and refund.origin == "late_capture"
    run_refunds(order)
    sync_messages()
    refund.refresh_from_db()
    payment.refresh_from_db()
    assert refund.state == "succeeded" and payment.status == "refunded"


# --- refunds -----------------------------------------------------------------------------------------------


def paid_order(client: APIClient, customer: Customer, now: datetime, **kwargs: Any) -> tuple[Order, Payment]:
    order = uzum_order(customer, now, **kwargs)
    payment = registered(client, order, now + timedelta(seconds=5))
    fake_uzum.complete(payment.provider_txn_id)
    assert uzum_flow.sync_payment(payment.pk) == "paid"
    order.refresh_from_db()
    return order, payment


def test_a_refund_is_accepted_then_closed_from_the_status(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order, payment = paid_order(customer_client, customer, now, fake={"uzum": {"refund": "pending"}})
    services.cancel_order(order.pk, "qaytarish", admin().pk)
    refund = Refund.objects.get(payment=payment)
    assert refund.state == "requested" and refund.manual_reason == ""
    run_refunds(order)
    refund.refresh_from_db()
    assert refund.state == "submitting" and refund.provider_state["operation_id"] == protocol.operation_id(refund.pk)
    assert refund.provider_state["accepted_at"]
    sync_messages()
    refund.refresh_from_db()
    assert refund.state == "submitting"  # Uzum has not finished it
    fake_uzum.finish_refund(payment.provider_txn_id, protocol.operation_id(refund.pk))
    assert uzum_flow.sync_payment(payment.pk) == "unchanged"
    refund.refresh_from_db()
    payment.refresh_from_db()
    assert refund.state == "succeeded" and payment.status == "refunded"
    assert OrderEvent.objects.filter(order=order, type="refund_succeeded").count() == 1


def test_a_lost_refund_answer_is_resent_with_the_same_operation_id(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order, payment = paid_order(customer_client, customer, now, fake={"uzum": {"refund": "unknown"}})
    services.cancel_order(order.pk, "qaytarish", admin().pk)
    run_refunds(order)
    refund = Refund.objects.get(payment=payment)
    assert refund.state == "submitting" and "accepted_at" not in refund.provider_state
    Order.objects.filter(pk=order.pk).update(metadata={})
    Refund.objects.filter(pk=refund.pk).update(last_attempt_at=refund.last_attempt_at - timedelta(minutes=10))
    uzum_flow.reconcile()  # resend → 3028 (the operation exists) → accepted; the sync closes it
    sync_messages()
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    assert [method for method, _ in fake_uzum.calls].count("refund") == 2


def test_a_refused_refund_fails_for_staff(customer_client: APIClient, customer: Customer, now: datetime) -> None:
    order, payment = paid_order(customer_client, customer, now, fake={"uzum": {"refund": "rejected"}})
    services.cancel_order(order.pk, "qaytarish", admin().pk)
    run_refunds(order)
    refund = Refund.objects.get(payment=payment)
    assert refund.state == "failed" and refund.error_code == "uzum_3021"


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_a_fake_uzum_under_the_production_settings_takes_no_payment_and_refunds_by_hand() -> None:
    assert "uzum" not in integration_modes.payable_providers()
    assert integration_modes.callbacks_refused("uzum")


# --- reconcile ---------------------------------------------------------------------------------------------


def test_reconcile_finds_a_capture_whose_callbacks_were_all_lost(
    customer_client: APIClient, customer: Customer, now: datetime
) -> None:
    order = uzum_order(customer, now)
    payment = registered(customer_client, order, now + timedelta(seconds=5))
    fake_uzum.complete(payment.provider_txn_id)
    assert uzum_flow.reconcile()["payments"] == 1
    payment.refresh_from_db()
    assert payment.status == "paid"


# --- every list knows every provider -----------------------------------------------------------------------


def test_every_payment_provider_is_in_every_provider_list() -> None:
    for provider in enums.PaymentProvider.values:
        assert provider in integration_modes.PAYMENT_PROVIDERS
        assert provider in integration_modes.PROVIDER_MODE_SETTINGS
        assert provider in integration_modes.CALLBACK_PROVIDERS
        assert provider in labels.PROVIDER
        assert provider in selectors.PROVIDER_BRANDS
        assert provider in WebhookInbox.Provider.values
        assert provider in IntegrationState.Provider.values
    assert integration_modes.PAYMENT_PROVIDERS[-1] == "uzum"  # released clients pick the first


def test_the_uzum_integration_state_row_exists() -> None:
    assert IntegrationState.objects.filter(provider="uzum").exists()
