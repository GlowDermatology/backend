"""The offline Payme Merchant API sandbox suite (plan §5.2; customer contract §2.13.2), row by row."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import payment_ref, services
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import admin, place, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import (
    PAYME_URL,
    at,
    ms,
    payme,
    prepare,
    real_now,
    ref,
    rows,
    tiyin,
)
from glowderma.integrations.payme import sandbox
from glowderma.loyalty.models import Purchase
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db
TXN = "5305e3bab097f420a62ced0b"


@pytest.fixture
def now() -> datetime:
    seed_policies()
    return real_now()


@pytest.fixture
def order(now: datetime) -> Order:
    return place(shopper(), fulfilment="pickup", provider="payme", when=now)


def _code(answer: dict[str, Any]) -> int:
    return int(answer["error"]["code"])


def _create(order: Order, now: datetime, txn: str = TXN, *, offset: int = 60) -> dict[str, Any]:
    with at(now + timedelta(seconds=offset)):
        return payme(sandbox.create(txn, ref(order), tiyin(order), ms(now)))


# --- rows 1-2: auth and envelope
# -------------------------------------------------------------------------------


def test_row_1_auth(order: Order) -> None:
    body = sandbox.check_perform(ref(order), tiyin(order))
    assert _code(payme(body, auth="")) == -32504
    assert _code(payme(body, key="wrong-key")) == -32504
    assert all(row.event_key.startswith("rejected:") and row.raw_body == "" for row in rows("payme"))
    assert rows("payme")[0].result["error_code"] == "bad_auth"


def test_row_2_envelope_errors(order: Order) -> None:
    assert _code(payme("{not json")) == -32700
    assert _code(payme({"jsonrpc": "2.0", "id": 1, "params": {}})) == -32600
    assert _code(payme({"jsonrpc": "2.0", "id": 1, "method": "Explode", "params": {}})) == -32601
    assert _code(payme({"jsonrpc": "2.0", "id": 1, "method": "ChangePassword", "params": {}})) == -32601
    response = APIClient().get(PAYME_URL)
    assert response.status_code == 200 and response.json()["error"]["code"] == -32300


# --- rows 3-7: CheckPerformTransaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["unknown", "wrong_hmac", "missing"])
def test_row_3_account_errors(order: Order, variant: str) -> None:
    account = {
        "unknown": {"order_id": payment_ref.build(10**8)},
        "wrong_hmac": {"order_id": f"{order.pk}-ffffffffff"},
        "missing": {},
    }[variant]
    answer = payme(sandbox.request("CheckPerformTransaction", {"amount": tiyin(order), "account": account}))
    assert _code(answer) == -31050 and answer["error"]["data"] == "order_id"


def test_row_3_account_field_setting(order: Order) -> None:
    with override_settings(PAYME_ACCOUNT_FIELD="invoice"):
        ok = payme(
            sandbox.request(
                "CheckPerformTransaction", {"amount": tiyin(order), "account": {"invoice": ref(order)}}
            )
        )
        assert ok["result"] == {"allow": True}
        bad = payme(sandbox.request("CheckPerformTransaction", {"amount": tiyin(order), "account": {}}))
        assert bad["error"]["data"] == "invoice"


@pytest.mark.parametrize("amount", ["wrong", 78440000.5, 0])
def test_row_4_amounts(order: Order, amount: Any) -> None:
    value = tiyin(order) + 100 if amount == "wrong" else amount
    assert _code(payme(sandbox.check_perform(ref(order), value))) == -31001


def test_row_5_not_payable(order: Order, now: datetime) -> None:
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires):
        assert _code(payme(sandbox.check_perform(ref(order), tiyin(order)))) == -31008
    services.cancel_by_customer(order.pk, order.customer_id)
    assert _code(payme(sandbox.check_perform(ref(order), tiyin(order)))) == -31008


def test_row_6_busy(order: Order, now: datetime) -> None:
    assert _create(order, now)["result"]["state"] == 1
    with at(now + timedelta(seconds=70)):
        answer = payme(sandbox.check_perform(ref(order), tiyin(order)))
    assert _code(answer) == -31051 and answer["error"]["data"] == "order_id"


def test_row_6_click_in_flight(now: datetime) -> None:
    order = place(shopper(), fulfilment="pickup", when=now)
    with at(now + timedelta(seconds=30)):
        assert prepare(order)["error"] == 0
    with at(now + timedelta(seconds=60)):
        assert _code(payme(sandbox.check_perform(ref(order), tiyin(order)))) == -31051


def test_row_7_valid(order: Order) -> None:
    assert payme(sandbox.check_perform(ref(order), tiyin(order)))["result"] == {"allow": True}
    assert rows("payme")[0].raw_body == ""  # read-only methods keep no raw body


# --- rows 8-12: CreateTransaction
# --------------------------------------------------------------------------------


def test_rows_8_9_create_and_replay(order: Order, now: datetime) -> None:
    payment = Payment.objects.get(order=order)
    first = _create(order, now)
    assert first["result"]["transaction"] == str(payment.pk) and first["result"]["state"] == 1
    again = _create(order, now, offset=90)
    assert again["result"] == first["result"]
    payment.refresh_from_db()
    assert payment.provider_txn_id == TXN and payment.provider_state["state"] == 1
    assert rows("payme")[0].raw_body  # a write method keeps its raw body
    assert all("Authorization" not in row.headers for row in rows("payme"))  # credentials never stored
    assert all(row.event_key.startswith("CreateTransaction:") for row in rows("payme"))


def test_row_10_other_id_while_state_1(order: Order, now: datetime) -> None:
    _create(order, now)
    answer = _create(order, now, "another-transaction-id", offset=70)
    assert _code(answer) == -31051
    assert Payment.objects.get(order=order).provider_txn_id == TXN


def test_row_11_stale_time(order: Order, now: datetime) -> None:
    with at(now + timedelta(minutes=1)):
        old = ms(now - timedelta(hours=13))
        assert _code(payme(sandbox.create(TXN, ref(order), tiyin(order), old))) == -31008


def test_row_12_new_id_after_expiry_within_the_grace(order: Order) -> None:
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires + timedelta(seconds=10)):
        assert _code(payme(sandbox.create(TXN, ref(order), tiyin(order), ms(expires)))) == -31008


# --- rows 13-16: PerformTransaction
# ------------------------------------------------------------------------------


def test_row_13_unknown_id(order: Order) -> None:
    assert _code(payme(sandbox.perform("nope"))) == -31003


def test_rows_14_15_perform_and_replay(order: Order, now: datetime) -> None:
    _create(order, now)
    with at(now + timedelta(minutes=2)):
        first = payme(sandbox.perform(TXN))
        again = payme(sandbox.perform(TXN))
    assert first["result"]["state"] == 2 and again["result"] == first["result"]
    order.refresh_from_db()
    assert order.status == "processing"
    assert Purchase.objects.filter(order_id=order.pk).count() == 1
    assert Payment.objects.get(order=order).status == "paid"


def test_row_16_perform_within_the_grace(order: Order, now: datetime) -> None:
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires - timedelta(seconds=30)):
        assert payme(sandbox.create(TXN, ref(order), tiyin(order), ms(expires)))["result"]["state"] == 1
    with at(expires + timedelta(seconds=120)):
        assert services.expire_due_reservations() == []
        assert payme(sandbox.perform(TXN))["result"]["state"] == 2


# --- rows 17-21: CancelTransaction
# --------------------------------------------------------------------------------


def _performed(order: Order, now: datetime) -> None:
    _create(order, now)
    with at(now + timedelta(minutes=2)):
        assert payme(sandbox.perform(TXN))["result"]["state"] == 2


def test_row_17_cancel_of_a_performed_transaction_without_a_refund(order: Order, now: datetime) -> None:
    _performed(order, now)
    assert _code(payme(sandbox.cancel(TXN, 5))) == -31007


def test_row_18_cabinet_refund_after_an_admin_cancel(order: Order, now: datetime) -> None:
    _performed(order, now)
    services.cancel_order(order.pk, "Mijoz so‘rovi", admin().pk)
    refund = Refund.objects.get(payment__order=order)
    assert refund.state == "manual_required"
    event = OrderEvent.objects.get(order=order, type="refund_requested")
    assert event.data["manual"] is True and event.data["manual_reason"] == "payme"
    answer = payme(sandbox.cancel(TXN, 5))
    assert answer["result"]["state"] == -2
    refund.refresh_from_db()
    assert refund.state == "succeeded"
    assert Payment.objects.get(order=order).status == "refunded"
    assert OrderEvent.objects.filter(order=order, type="refund_succeeded").exists()


def test_rows_19_20_21_cancel_in_state_1(order: Order, now: datetime) -> None:
    _create(order, now)
    with at(now + timedelta(minutes=2)):
        first = payme(sandbox.cancel(TXN, 3))
        again = payme(sandbox.cancel(TXN, 3))
        assert _code(payme(sandbox.perform(TXN))) == -31008
    assert first["result"]["state"] == -1 and again["result"] == first["result"]
    payment = Payment.objects.get(order=order)
    assert (payment.status, payment.error_code) == ("failed", "3")
    assert OrderEvent.objects.filter(order=order, type="payment_failed").count() == 1


# --- rows 22-26
# ---------------------------------------------------------------------------------------------------


def test_row_22_timeout_cancels_on_the_next_touch(order: Order, now: datetime) -> None:
    _create(order, now)
    payment = Payment.objects.get(order=order)
    payment.provider_state = {**payment.provider_state, "create_time": ms(now - timedelta(hours=13))}
    payment.save(update_fields=["provider_state"])
    with at(now + timedelta(minutes=2)):
        assert _code(payme(sandbox.perform(TXN))) == -31008
    payment.refresh_from_db()
    assert payment.provider_state["state"] == -1 and payment.provider_state["reason"] == 4
    assert payment.status == "failed"


def test_row_23_perform_after_the_grace(order: Order, now: datetime) -> None:
    _create(order, now)
    expires = order.reservation_expires_at
    assert expires is not None
    with at(expires + timedelta(seconds=301)):
        services.expire_due_reservations()
        assert _code(payme(sandbox.perform(TXN))) == -31008
    assert Payment.objects.get(order=order).status == "pending"  # Payme cancels it


def test_row_24_check_transaction(order: Order, now: datetime) -> None:
    _create(order, now)
    payment = Payment.objects.get(order=order)
    created = payme(sandbox.check(TXN))["result"]
    assert created["state"] == 1 and created["perform_time"] == 0 and created["cancel_time"] == 0
    assert created["reason"] is None and created["transaction"] == str(payment.pk)
    with at(now + timedelta(minutes=2)):
        payme(sandbox.cancel(TXN, 3))
    cancelled = payme(sandbox.check(TXN))["result"]
    assert cancelled["state"] == -1 and cancelled["reason"] == 3 and cancelled["cancel_time"] > 0


def test_row_25_statement_by_payme_time(now: datetime) -> None:
    first = place(shopper(), fulfilment="pickup", provider="payme", when=now)
    second = place(shopper(), fulfilment="pickup", provider="payme", when=now)
    with at(now + timedelta(minutes=1)):
        payme(sandbox.create("txn-b", ref(second), tiyin(second), ms(now) - 5000))
        payme(sandbox.create("txn-a", ref(first), tiyin(first), ms(now) - 9000))
        payme(sandbox.cancel("txn-a", 3))
    statement = payme(sandbox.statement(ms(now) - 10_000, ms(now)))["result"]["transactions"]
    assert [row["id"] for row in statement] == ["txn-a", "txn-b"]
    assert statement[0]["account"] == {"order_id": ref(first)} and statement[0]["state"] == -1


def test_row_26_an_exception_answers_32400(order: Order, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "lock_order", boom)
    assert _code(payme(sandbox.check_perform(ref(order), tiyin(order)))) == -32400
    assert rows("payme") == []


def test_row_27_fake_payme_in_production(order: Order) -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, PAYME_MODE="fake"):
        answer = payme(sandbox.check_perform(ref(order), tiyin(order)))
    assert _code(answer) == -32504
    (row,) = rows("payme")
    assert row.result["error_code"] == "fake_mode" and row.event_key.startswith("rejected:")


def test_cancel_of_a_performed_transaction_while_payme_is_fake(order: Order, now: datetime) -> None:
    _performed(order, now)
    services.cancel_order(order.pk, "Mijoz so‘rovi", admin().pk)
    with override_settings(DEV_FEATURES_ALLOWED=False, PAYME_MODE="fake"):
        assert _code(payme(sandbox.cancel(TXN, 5))) == -32504
    assert Refund.objects.get(payment__order=order).state == "manual_required"
