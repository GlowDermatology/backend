"""The offline Click SHOP API sandbox suite (plan §5.1; customer contract §2.13.0–§2.13.1), row by row.

Real webhook views, signatures built by ``integrations.click.sandbox``, a clock pinned relative to the real now.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import payment_ref, services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import deliver, place, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import (
    CLICK_COMPLETE,
    CLICK_PREPARE,
    TXN,
    at,
    click,
    complete,
    complete_params,
    keys,
    payments,
    payme,
    prepare,
    prepare_params,
    real_now,
    ref,
    rows,
    tiyin,
)
from glowderma.core import enums
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.payme import sandbox as payme_sandbox
from glowderma.loyalty.models import Purchase
from glowderma.payments.models import Payment, Refund
from glowderma.payments.selectors import refunds_needing_action

pytestmark = pytest.mark.django_db
PROD_FAKE_CLICK = {"DEV_FEATURES_ALLOWED": False, "CLICK_MODE": "fake"}


@pytest.fixture
def now() -> datetime:
    seed_policies()
    return real_now()


@pytest.fixture
def order(now: datetime) -> Order:
    return place(shopper(), fulfilment="pickup", when=now)


def _prepared(order: Order, now: datetime, txn: int = TXN, seconds: int = 60) -> int:
    with at(now + timedelta(seconds=seconds)):
        answer = prepare(order, txn)
    assert answer["error"] == 0, answer
    return int(answer["merchant_prepare_id"])


# --- Prepare ---------------------------------------------------------------------------------------------------


def test_row_1_prepare_attaches_the_transaction(order: Order, now: datetime) -> None:
    payment = payments(order)[0]
    with at(now + timedelta(minutes=1)):
        answer = prepare(order)
    assert answer == {
        "click_trans_id": TXN,
        "merchant_trans_id": ref(order),
        "merchant_prepare_id": payment.pk,
        "error": 0,
        "error_note": "Success",
    }
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.provider_txn_id == str(TXN)
    assert payment.provider_state["click_paydoc_id"] == str(TXN + 1_000_000)
    assert payment.provider_state["prepare_at"] and payment.provider_state["merchant_prepare_id"] == payment.pk
    assert keys("click") == [f"prepare:{TXN}"]
    row = rows("click")[0]
    assert row.result["outcome"] == "accepted" and row.entity_ref == f"payment:{payment.pk}"
    assert "Authorization" not in row.headers and row.raw_body
    assert not OrderEvent.objects.filter(order=order, type__startswith="payment").exists()  # no event


def test_rows_2_3_replays_answer_from_state_with_dup_rows(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=2)):
        assert click(CLICK_PREPARE, prepare_params(order))["merchant_prepare_id"] == payment_id
        resigned = prepare(order, sign_time="2026-10-23 14:09:59")
    assert resigned["error"] == 0 and resigned["merchant_prepare_id"] == payment_id
    found = keys("click")
    assert found[0] == f"prepare:{TXN}" and len(found) == 3
    assert all(key.startswith(f"dup:prepare:{TXN}:") for key in found[1:])
    assert all(row.result["outcome"] == "duplicate" and row.raw_body == "" for row in rows("click")[1:])
    assert len(payments(order)) == 1


def test_row_4_wrong_signature_stores_a_bounded_rejected_row(order: Order, now: datetime) -> None:
    params = prepare_params(order)
    params["sign_string"] = "0" * 32
    params["error_note"] = "x" * 3000
    answer = click(CLICK_PREPARE, params)
    assert answer["error"] == -1 and answer["error_note"] == "SIGN CHECK FAILED!"
    assert answer["merchant_prepare_id"] is None
    (row,) = rows("click")
    assert row.event_key.startswith("rejected:") and row.raw_body == ""
    assert isinstance(row.payload, str) and len(row.payload) <= 1024
    assert row.result["outcome"] == "rejected" and row.result["error_code"] == "bad_signature"
    assert payments(order)[0].provider_txn_id == ""


def test_row_5_complete_action_on_the_prepare_url(order: Order) -> None:
    assert prepare(order, action="1")["error"] == -3


@pytest.mark.parametrize("variant", ["unknown", "wrong_hmac", "bare"])
def test_row_6_unknown_order_or_reference(order: Order, variant: str) -> None:
    reference = {
        "unknown": payment_ref.build(10**8),
        "wrong_hmac": f"{order.pk}-0000000000",
        "bare": str(order.pk),
    }[variant]
    assert prepare(order, merchant_trans_id=reference)["error"] == -5
    assert payments(order)[0].provider_txn_id == ""


@pytest.mark.parametrize("amount", ["+1", "784400.50", "abc"])
def test_row_7_bad_amounts(order: Order, amount: str) -> None:
    value = str(order.total + 1) if amount == "+1" else amount
    assert prepare(order, amount=value)["error"] == -2
    assert payments(order)[0].provider_txn_id == ""


def test_row_7_amount_with_two_zero_decimals_is_accepted(order: Order) -> None:
    assert prepare(order, amount=f"{order.total}.00")["error"] == 0


def test_row_8_malformed_requests(order: Order, now: datetime) -> None:
    assert prepare(order, service_id="99999")["error"] == -8
    params = prepare_params(order)
    del params["sign_time"]
    assert click(CLICK_PREPARE, params)["error"] == -8
    params = prepare_params(order)
    del params["click_paydoc_id"]
    assert click(CLICK_PREPARE, params)["error"] == -8
    assert click(CLICK_PREPARE, prepare_params(order), content_type="application/json")["error"] == -8
    _prepared(order, now)
    conflicting = prepare(order, amount=order.total + 5)
    assert conflicting["error"] == -8
    assert rows("click")[-1].event_key.startswith("rejected:")
    assert rows("click")[-1].result["error_code"] == "fingerprint_conflict"


def test_row_9_cancelled_or_expired_order(order: Order, now: datetime) -> None:
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires):
        assert prepare(order)["error"] == -9
    other = place(shopper(), fulfilment="pickup", when=now)
    services.cancel_by_customer(other.pk, other.customer_id)
    assert prepare(other, txn=TXN + 1)["error"] == -9


def test_row_10_paid_order(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=2)):
        assert complete(order, payment_id)["error"] == 0
        assert prepare(order, txn=TXN + 7)["error"] == -4


@pytest.mark.parametrize("age", [30, 600])
def test_rows_11_12_a_new_prepare_supersedes_and_the_old_complete_is_reversed(
    order: Order, now: datetime, age: int
) -> None:
    old_id = _prepared(order, now, seconds=10)
    later = now + timedelta(seconds=10 + age)
    assert later < order.reservation_expires_at  # type: ignore[operator]
    with at(later):
        answer = prepare(order, txn=TXN + 1)
    assert answer["error"] == 0 and answer["merchant_prepare_id"] != old_id
    old = Payment.objects.get(pk=old_id)
    assert (old.status, old.error_code) == ("failed", "abandoned")
    new = Payment.objects.get(pk=answer["merchant_prepare_id"])
    assert new.status == "pending" and new.provider_txn_id == str(TXN + 1)
    assert not OrderEvent.objects.filter(order=order, type="payment_failed").exists()
    with at(later + timedelta(seconds=5)):
        reversed_ = complete(order, old_id)
    assert reversed_["error"] == 0 and reversed_["merchant_confirm_id"] == old_id
    old.refresh_from_db()
    assert (old.status, old.error_code) == ("paid", "late_capture")
    refund = Refund.objects.get(payment=old)
    assert refund.origin == "late_capture"
    audit = AuditEntry.objects.get(action="payment.late_capture")
    assert audit.after["case"] == "failed_by_us"
    new.refresh_from_db()
    assert new.status == "pending"
    order.refresh_from_db()
    assert order.status == "awaiting_payment"
    with at(later + timedelta(seconds=6)):
        assert complete(order, new.pk, txn=TXN + 1)["error"] == 0  # the new payment is still payable
    order.refresh_from_db()
    assert order.status == "processing"


def test_row_13_prepare_while_payme_is_in_state_1(now: datetime) -> None:
    order = place(shopper(), fulfilment="pickup", provider="payme", when=now)
    with at(now + timedelta(minutes=1)):
        created = payme(payme_sandbox.create("payme-txn-1", ref(order), tiyin(order), ms_(now)))
        assert created["result"]["state"] == 1
        assert prepare(order)["error"] == -9


def ms_(instant: datetime) -> int:
    return int(instant.timestamp() * 1000)


# --- Complete ----------------------------------------------------------------------------------------------------


def test_row_14_complete_pays_the_order(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=2)):
        answer = complete(order, payment_id)
    assert answer["error"] == 0 and answer["merchant_confirm_id"] == payment_id
    order.refresh_from_db()
    assert order.status == "processing"
    payment = Payment.objects.get(pk=payment_id)
    assert payment.status == "paid" and payment.error_code == ""
    assert payment.provider_state["complete_at"] and payment.provider_state["error"] == "0"
    assert OrderEvent.objects.filter(order=order, type="payment_confirmed").count() == 1
    assert OutboxMessage.objects.filter(topic="push.order_status", payload__order_id=order.pk).exists()


def test_rows_15_16_complete_replays_have_one_effect(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=2)):
        assert complete(order, payment_id)["error"] == 0
        assert click(CLICK_COMPLETE, complete_params(order, payment_id))["error"] == 0
        assert complete(order, payment_id, sign_time="2026-10-23 14:30:00")["merchant_confirm_id"] == payment_id
    assert Purchase.objects.filter(order_id=order.pk).count() == 1
    assert OrderEvent.objects.filter(order=order, type="payment_confirmed").count() == 1
    completes = [key for key in keys("click") if "complete" in key]
    assert completes[0] == f"complete:{TXN}" and len(completes) == 3
    assert all(key.startswith(f"dup:complete:{TXN}:") for key in completes[1:])


def test_row_17_unknown_prepare_id(order: Order, now: datetime) -> None:
    _prepared(order, now)
    assert complete(order, 10**8)["error"] == -6


def test_row_18_click_reports_a_failure(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=2)):
        assert complete(order, payment_id, error=-5017)["error"] == -9
    payment = Payment.objects.get(pk=payment_id)
    assert (payment.status, payment.error_code) == ("failed", "-5017")
    event = OrderEvent.objects.get(order=order, type="payment_failed")
    assert event.data == {"provider": "click", "transaction_id": str(TXN), "error_code": "-5017"}
    order.refresh_from_db()
    assert order.status == "awaiting_payment"
    with at(now + timedelta(minutes=3)):  # row 30: a repeated Complete of a payment Click failed
        assert complete(order, payment_id, error=-5017, sign_time="2026-10-23 14:20:00")["error"] == -9
        assert complete(order, payment_id)["error"] == -8  # same transaction, another error: conflicting
    assert Payment.objects.get(pk=payment_id).status == "failed"


def test_row_19_amount_differs(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    assert complete(order, payment_id, amount=order.total + 1)["error"] == -2
    assert Payment.objects.get(pk=payment_id).status == "pending"


def test_row_20_complete_within_the_grace(order: Order, now: datetime) -> None:
    expires = order.reservation_expires_at
    assert expires is not None
    payment_id = _prepared(order, now, seconds=int((expires - now).total_seconds()) - 30)
    with at(expires + timedelta(seconds=200)):  # Click left the 120 s in-flight window, still open
        assert services.expire_due_reservations() == []  # the job skips it during the grace
        assert complete(order, payment_id)["error"] == 0
    order.refresh_from_db()
    assert order.status == "processing"


def test_row_21_complete_after_the_grace_is_a_refused_capture(order: Order, now: datetime) -> None:
    expires = order.reservation_expires_at
    assert expires is not None
    payment_id = _prepared(order, now)
    with at(expires + timedelta(seconds=301)):
        assert services.expire_due_reservations() == [order.pk]
        answer = complete(order, payment_id)
    assert answer["error"] == 0 and answer["merchant_confirm_id"] == payment_id
    payment = Payment.objects.get(pk=payment_id)
    assert (payment.status, payment.error_code, payment.failed_at) == ("paid", "late_capture", None)
    refund = Refund.objects.get(payment=payment)
    assert (refund.origin, refund.state) == ("late_capture", "requested")
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert event.data["manual"] is False and event.data["manual_reason"] is None and event.source == "system"
    assert AuditEntry.objects.get(action="payment.late_capture").after == {
        "order_id": order.pk,
        "payment_id": payment_id,
        "provider": "click",
        "case": "failed_by_us",
    }
    order.refresh_from_db()
    assert order.status == "cancelled"
    deliver("refund.submit", order.pk)
    assert ("refund", str(TXN + 1_000_000)) in fake_click.calls  # keyed by click_paydoc_id
    assert all(ref_ != str(TXN) for _call, ref_ in fake_click.calls)
    refund.refresh_from_db()
    assert refund.state == "succeeded"


def test_row_25_complete_after_a_switch_to_payme(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=4)):  # Click left the in-flight window: the switch abandons it
        payme_payment = services.ensure_payable_payment(order.pk, "payme")
        assert complete(order, payment_id)["error"] == 0
    assert AuditEntry.objects.get(action="payment.late_capture").after["case"] == "failed_by_us"
    assert Payment.objects.get(pk=payme_payment.id).status == "pending"


def test_row_26_double_payment(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=4)):
        payme_payment = services.ensure_payable_payment(order.pk, "payme")
        services.mark_paid(order.pk, payme_payment.id, "payme", "payme-paid-1")
        assert complete(order, payment_id)["error"] == 0
    click_payment = Payment.objects.get(pk=payment_id)
    assert (click_payment.status, click_payment.error_code) == ("paid", "late_capture")
    assert Payment.objects.get(pk=payme_payment.id).status == "paid"
    assert AuditEntry.objects.get(action="payment.late_capture").after["case"] == "double_payment"
    order.refresh_from_db()
    assert order.status == "processing"
    detail = services.selectors.order_detail(order.pk)
    assert detail is not None and detail.payment["provider"] == "payme"


def test_row_28_complete_after_a_customer_cancel(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=4)):
        services.cancel_by_customer(order.pk, order.customer_id)
        assert complete(order, payment_id)["error"] == 0
    assert AuditEntry.objects.get(action="payment.late_capture").after["case"] == "failed_by_us"


def test_row_29_bad_signature_or_tampered_prepare_id(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    params = complete_params(order, payment_id)
    params["merchant_prepare_id"] = str(payment_id + 1)  # tampered after signing
    assert click(CLICK_COMPLETE, params)["error"] == -1
    assert Payment.objects.get(pk=payment_id).status == "pending"


def test_row_31_complete_of_a_refunded_payment_answers_from_state(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    with at(now + timedelta(minutes=2)):
        assert complete(order, payment_id)["error"] == 0
    staff = __import__("glowderma.commerce.tests.support", fromlist=["admin"]).admin()
    services.cancel_order(order.pk, "Mijoz so‘rovi", staff.pk)
    deliver("refund.submit", order.pk)
    assert Payment.objects.get(pk=payment_id).status == "refunded"
    before = rows("click")
    assert complete(order, payment_id)["error"] == 0
    after = rows("click")
    assert len(after) == len(before) + 1 and after[-1].event_key.startswith("dup:complete:")


def _refused_reversal(now: datetime, status: str | None = None) -> tuple[Order, Refund]:
    order = place(shopper(), fulfilment="pickup", when=now, fake={"click": {"refund": "failed"}})
    payment_id = _prepared(order, now)
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires + timedelta(seconds=400)):
        services.expire_due_reservations()
        assert complete(order, payment_id)["error"] == 0
    deliver("refund.submit", order.pk)
    refund = Refund.objects.get(payment_id=payment_id)
    assert refund.state == "failed"
    if status:  # what Click's status call answers later (a reversal it performed after all)
        order.metadata = {"fake": {"click": {"refund": "failed", "status": status}}}
        order.save(update_fields=["metadata"])
    return order, refund


def test_row_32_a_refused_reversal_needs_a_staff_payout(now: datetime, staff_client: APIClient) -> None:
    order, refund = _refused_reversal(now)
    assert refunds_needing_action() == 1
    url = f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/resolve/"
    refused = staff_client.post(url, {"state": "succeeded"}, format="json")
    assert refused.status_code == 409 and refused.json()["code"] == "invalid_transition"
    assert refused.json()["message"] == services.MESSAGE_REFUND_STILL_PAID
    paid = staff_client.post(url, {"state": "succeeded", "manual_payout": True}, format="json")
    assert paid.status_code == 200
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    audit = AuditEntry.objects.filter(action="refund.state_changed", entity_id=str(refund.pk)).last()
    assert audit is not None and audit.after["manual_payout"] is True and audit.after["from"] == "failed"
    assert refunds_needing_action() == 0


def test_row_32_click_reversed_after_all_closes_without_a_payout(now: datetime, staff_client: APIClient) -> None:
    _order, refund = _refused_reversal(now, status="reversed")
    url = f"/api/v1/admin/payments/{refund.payment_id}/refunds/{refund.pk}/resolve/"
    assert staff_client.post(url, {"state": "succeeded"}, format="json").status_code == 200
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    audit = AuditEntry.objects.filter(action="refund.state_changed", entity_id=str(refund.pk)).last()
    assert audit is not None and audit.after["manual_payout"] is False


# --- fake Click under the production settings (rows 33, 34) --------------------------------------------------------


def test_row_33_fake_click_in_production_refuses_everything(order: Order, now: datetime) -> None:
    with override_settings(**PROD_FAKE_CLICK), at(now + timedelta(minutes=1)):
        assert prepare(order)["error"] == -1
        assert complete(order, payments(order)[0].pk)["error"] == -1  # never attached
    assert payments(order)[0].provider_txn_id == "" and Order.objects.get(pk=order.pk).status == "awaiting_payment"
    assert [row.result["error_code"] for row in rows("click")] == ["fake_mode", "fake_mode"]
    assert all(row.event_key.startswith("rejected:") for row in rows("click"))


def test_row_34_complete_of_a_transaction_attached_while_live(
    order: Order, now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    payment_id = _prepared(order, now)
    with (
        override_settings(**PROD_FAKE_CLICK),
        at(now + timedelta(minutes=2)),
        caplog.at_level(logging.ERROR, logger="glowderma.commerce.services"),
    ):
        bad = complete_params(order, payment_id)
        bad["sign_string"] = "f" * 32
        assert click(CLICK_COMPLETE, bad)["error"] == -1
        answer = complete(order, payment_id)
    assert answer["error"] == 0 and answer["merchant_confirm_id"] == payment_id
    payment = Payment.objects.get(pk=payment_id)
    assert (payment.status, payment.error_code) == ("paid", "late_capture")
    refund = Refund.objects.get(payment=payment)
    assert refund.state == "manual_required"
    assert not OutboxMessage.objects.filter(topic="refund.submit").exists()
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert event.data["manual"] is True and event.data["manual_reason"] == "fake_mode"
    assert AuditEntry.objects.get(action="payment.late_capture").after["case"] == "fake_mode"
    assert "payment.late_capture" in caplog.text
    order.refresh_from_db()
    assert order.status == "awaiting_payment"
    assert fake_click.calls == []


def test_row_34_variant_the_order_expired_first(order: Order, now: datetime) -> None:
    payment_id = _prepared(order, now)
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires + timedelta(seconds=400)):
        services.expire_due_reservations()
    assert Payment.objects.get(pk=payment_id).error_code == "expired"
    with override_settings(**PROD_FAKE_CLICK), at(expires + timedelta(seconds=401)):
        assert complete(order, payment_id)["error"] == 0
    assert AuditEntry.objects.get(action="payment.late_capture").after["case"] == "failed_by_us"
    assert Refund.objects.get(payment_id=payment_id).state == "manual_required"


def test_an_unexpected_exception_answers_500_and_rolls_back(order: Order, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(services, "ensure_payable_payment", boom)
    response = APIClient().generic(
        "POST", CLICK_PREPARE, urlencode(prepare_params(order)), content_type="application/x-www-form-urlencoded"
    )
    assert response.status_code == 500
    assert rows("click") == [] and payments(order)[0].provider_txn_id == ""
