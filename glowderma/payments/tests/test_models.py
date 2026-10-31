"""payments models (design 02 §2, 03 §10): every CHECK / partial unique through IntegrityError, the FK
``on_delete`` rules, ordering, ``__str__`` and the indexes/constraints as PostgreSQL stores them."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.payments.models import (
    ACTIVE_PAYMENT_STATUSES,
    ATTENTION_REFUND_STATES,
    OPEN_REFUND_STATES,
    SETTLED_PAYMENT_STATUSES,
    SUBMITTED_REFUND_STATES,
    Payment,
    Refund,
)
from glowderma.payments.tests.factories import OrderRowFactory, PaymentFactory, RefundFactory

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 10, 23, 14, 2, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def check_clauses(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = %s::regclass AND contype = 'c'",
            [table],
        )
        return dict(cursor.fetchall())


# --- state tables ------------------------------------------------------------------------------------------


def test_state_tables_match_the_design() -> None:
    assert ACTIVE_PAYMENT_STATUSES == ("pending", "paid")
    assert SETTLED_PAYMENT_STATUSES == ("paid", "refunded")
    assert OPEN_REFUND_STATES == ("requested", "submitting", "manual_required", "unknown")
    assert ATTENTION_REFUND_STATES == ("failed", "unknown", "manual_required")
    assert SUBMITTED_REFUND_STATES == ("submitting", "unknown")
    assert set(OPEN_REFUND_STATES) | {"succeeded", "failed"} == set(enums.RefundState.values)
    assert Refund.Origin.values == ["order_cancelled", "late_capture", "manual"]


# --- Payment -----------------------------------------------------------------------------------------------


def test_payment_factory_defaults_str_and_traits() -> None:
    payment = PaymentFactory.create(order__created_at=T0)
    assert payment.status == enums.PaymentStatus.PAID and payment.provider == enums.PaymentProvider.CLICK
    assert payment.amount == payment.order.total == 210_000
    assert payment.provider_txn_id.startswith("CLK-") and payment.confirmed_at == T0 + timedelta(minutes=3)
    assert payment.failed_at is None and payment.refunded_at is None
    assert payment.provider_state == {} and payment.error_code == "" and payment.error_message == ""
    assert (
        str(payment)
        == f"To‘lov #{payment.pk} Click · To‘langan · 210\u00a0000 so‘m (buyurtma №{payment.order_id})"
    )
    pending = PaymentFactory.create(pending=True)
    assert pending.status == "pending" and pending.provider_txn_id == "" and pending.confirmed_at is None
    assert pending.order.status == enums.OrderStatus.AWAITING_PAYMENT
    failed = PaymentFactory.create(failed=True)
    assert failed.status == "failed" and failed.failed_at is not None and failed.error_code == "-9"
    refunded = PaymentFactory.create(refunded=True)
    assert refunded.status == "refunded" and refunded.refunded_at is not None
    assert refunded.order.status == enums.OrderStatus.CANCELLED
    payme = PaymentFactory.create(order__payme=True)
    assert payme.provider == "payme" and len(payme.provider_txn_id) == 24


def test_payment_one_active_per_order() -> None:
    order = OrderRowFactory.create(paid=True)
    PaymentFactory.create(order=order)
    violates("payment_one_active_per_order", PaymentFactory.create, order=order)
    violates("payment_one_active_per_order", PaymentFactory.create, pending=True, order=order)
    # closed rows do not count: a failed attempt next to the paid one, and a refunded one next to a pending
    failed = PaymentFactory.create(failed=True, order=order)
    assert failed.order_id == order.pk
    second = OrderRowFactory.create(cancelled=True)
    PaymentFactory.create(refunded=True, order=second)
    assert PaymentFactory.create(pending=True, order=second).order_id == second.pk
    assert Payment.objects.filter(order=second).count() == 2


def test_payment_provider_txn_unique_where_non_empty() -> None:
    first = PaymentFactory.create(provider_txn_id="CLK-7741920385")
    violates("payment_provider_txn_unique", PaymentFactory.create, provider_txn_id="CLK-7741920385")
    # another provider may carry the same id; empty ids never collide
    other = PaymentFactory.create(order__payme=True, provider_txn_id="CLK-7741920385")
    assert other.provider == "payme" and first.provider == "click"
    PaymentFactory.create(pending=True)
    PaymentFactory.create(pending=True)
    assert Payment.objects.filter(provider_txn_id="").count() == 2


def test_payment_amount_positive() -> None:
    violates("payment_amount_positive", PaymentFactory.create, amount=0)
    violates("payment_amount_positive", PaymentFactory.create, amount=-1)
    assert PaymentFactory.create(amount=1).amount == 1


def test_payment_status_times() -> None:
    now = T0
    violates("payment_status_times", PaymentFactory.create, pending=True, confirmed_at=now)
    violates("payment_status_times", PaymentFactory.create, pending=True, failed_at=now)
    violates("payment_status_times", PaymentFactory.create, pending=True, refunded_at=now)
    violates("payment_status_times", PaymentFactory.create, confirmed_at=None)
    violates("payment_status_times", PaymentFactory.create, failed_at=now)
    violates("payment_status_times", PaymentFactory.create, refunded_at=now)
    violates("payment_status_times", PaymentFactory.create, failed=True, failed_at=None)
    violates("payment_status_times", PaymentFactory.create, failed=True, confirmed_at=now)
    violates("payment_status_times", PaymentFactory.create, failed=True, refunded_at=now)
    violates("payment_status_times", PaymentFactory.create, refunded=True, refunded_at=None)
    violates("payment_status_times", PaymentFactory.create, refunded=True, confirmed_at=None)
    violates("payment_status_times", PaymentFactory.create, refunded=True, failed_at=now)
    violates("payment_status_times", PaymentFactory.create, status="bogus")


def test_payment_paid_has_txn() -> None:
    violates("payment_paid_has_txn", PaymentFactory.create, provider_txn_id="")
    violates("payment_paid_has_txn", PaymentFactory.create, refunded=True, provider_txn_id="")
    # a pending payment with a transaction attached (Click prepare) and a failed one with an id are fine
    assert PaymentFactory.create(pending=True, provider_txn_id="CLK-1").provider_txn_id == "CLK-1"
    assert PaymentFactory.create(failed=True, provider_txn_id="CLK-2").status == "failed"
    assert PaymentFactory.create(failed=True, provider_txn_id="").provider_txn_id == ""


def test_payment_ordering_and_order_protect() -> None:
    order = OrderRowFactory.create(paid=True, created_at=T0)
    older = PaymentFactory.create(failed=True, order=order, created_at=T0)
    newer = PaymentFactory.create(order=order, created_at=T0 + timedelta(minutes=1))
    tie = PaymentFactory.create(failed=True, order=order, created_at=T0 + timedelta(minutes=1))
    assert list(Payment.objects.all()) == [tie, newer, older]
    assert list(order.payments.all()) == [tie, newer, older]
    with pytest.raises(ProtectedError):
        order.delete()
    assert Payment.objects.filter(order=order).count() == 3


def test_payment_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("payments_payment")
    assert "(order_id) WHERE (((status)::text = ANY" in indexes["payment_one_active_per_order"]
    assert "'late_capture'" in indexes["payment_one_active_per_order"]  # payments.0002
    assert indexes["payment_one_active_per_order"].startswith("CREATE UNIQUE INDEX")
    assert "'pending'" in indexes["payment_one_active_per_order"]
    assert "'paid'" in indexes["payment_one_active_per_order"]
    assert indexes["payment_provider_txn_unique"].startswith("CREATE UNIQUE INDEX")
    assert (
        "(provider, provider_txn_id) WHERE (NOT ((provider_txn_id)::text = ''::text))"
        in (indexes["payment_provider_txn_unique"])
    )
    assert "(order_id, id)" in indexes["payment_order_idx"]
    assert "(created_at, id)" in indexes["payment_created_idx"]
    assert "(status, created_at)" in indexes["payment_status_created_idx"]
    assert "(confirmed_at) WHERE" in indexes["payment_confirmed_idx"]
    assert "'refunded'" in indexes["payment_confirmed_idx"]
    assert any("provider_txn_id" in d and "_like" in n for n, d in indexes.items())  # db_index=True
    checks = check_clauses("payments_payment")
    assert checks["payment_amount_positive"] == "CHECK ((amount > 0))"
    assert "confirmed_at IS NOT NULL" in checks["payment_status_times"]
    assert "refunded_at IS NOT NULL" in checks["payment_status_times"]
    assert "NOT ((provider_txn_id)::text = ''::text)" in checks["payment_paid_has_txn"]
    assert set(checks) == {"payment_amount_positive", "payment_status_times", "payment_paid_has_txn"}


# --- Refund ------------------------------------------------------------------------------------------------


def test_refund_factory_defaults_str_and_traits() -> None:
    refund = RefundFactory.create()
    assert refund.state == enums.RefundState.REQUESTED and refund.origin == Refund.Origin.ORDER_CANCELLED
    assert refund.amount == refund.payment.amount == 210_000
    assert refund.requested_by is None and refund.attempts == 0 and refund.provider_state == {}
    assert refund.submitted_at is None and refund.completed_at is None and refund.last_attempt_at is None
    assert refund.payment.confirmed_at is not None
    assert refund.requested_at == refund.payment.confirmed_at + timedelta(hours=1)
    assert (
        str(refund)
        == f"Qaytarish #{refund.pk} · Qaytarish so‘raldi · 210\u00a0000 so‘m (to‘lov #{refund.payment_id})"
    )
    submitting = RefundFactory.create(submitting=True)
    assert submitting.submitted_at is not None and submitting.attempts == 1
    succeeded = RefundFactory.create(succeeded=True)
    assert succeeded.completed_at is not None and succeeded.provider_refund_id.startswith("RF-CLK-")
    failed = RefundFactory.create(failed=True)
    assert failed.error_code == "-31050" and failed.submitted_at is not None
    dead = RefundFactory.create(dead_letter=True)
    assert dead.state == "failed" and dead.error_code == "dead_letter" and dead.submitted_at is None
    manual = RefundFactory.create(manual_required=True)
    assert manual.payment.provider == "payme" and manual.submitted_at is None
    unknown = RefundFactory.create(unknown=True)
    assert unknown.submitted_at is not None and unknown.completed_at is None
    actor = StaffUserFactory.create()
    retry = RefundFactory.create(payment=failed.payment, origin=Refund.Origin.MANUAL, requested_by=actor)
    assert retry.requested_by == actor and retry.origin == "manual"


def test_refund_one_open_per_payment() -> None:
    payment = PaymentFactory.create()
    RefundFactory.create(payment=payment)
    for trait in ("submitting", "manual_required", "unknown"):
        violates("refund_one_open_per_payment", RefundFactory.create, payment=payment, **{trait: True})
    violates("refund_one_open_per_payment", RefundFactory.create, payment=payment)
    # closed rows never block: a failed refund, then a retry; a succeeded one next to a manual retry
    other = PaymentFactory.create()
    RefundFactory.create(failed=True, payment=other)
    RefundFactory.create(dead_letter=True, payment=other)
    retry = RefundFactory.create(payment=other, origin=Refund.Origin.MANUAL)
    assert retry.state == "requested"
    violates("refund_one_open_per_payment", RefundFactory.create, payment=other, unknown=True)
    third = PaymentFactory.create()
    RefundFactory.create(succeeded=True, payment=third)
    assert RefundFactory.create(payment=third).payment_id == third.pk


def test_refund_amount_positive() -> None:
    violates("refund_amount_positive", RefundFactory.create, amount=0)
    violates("refund_amount_positive", RefundFactory.create, amount=-5)
    # amount ≤ payment.amount is a service invariant (02 §2.2), not a CHECK: a row cannot see its payment
    assert RefundFactory.create(amount=1).amount == 1


def test_refund_completed_at() -> None:
    now = T0
    violates("refund_completed_at", RefundFactory.create, succeeded=True, completed_at=None)
    for trait in ("submitting", "failed", "manual_required", "unknown"):
        violates("refund_completed_at", RefundFactory.create, completed_at=now, **{trait: True})
    violates("refund_completed_at", RefundFactory.create, completed_at=now)


def test_refund_submitted_at() -> None:
    violates("refund_submitted_at", RefundFactory.create, submitting=True, submitted_at=None)
    violates("refund_submitted_at", RefundFactory.create, unknown=True, submitted_at=None)
    # requested/failed/succeeded/manual_required do not require it, and may carry it (a failed submission)
    assert RefundFactory.create(failed=True).submitted_at is not None
    assert RefundFactory.create(dead_letter=True).submitted_at is None
    assert RefundFactory.create(submitted_at=T0).state == "requested"


def test_refund_ordering_and_protect_rules() -> None:
    payment = PaymentFactory.create()
    older = RefundFactory.create(failed=True, payment=payment, requested_at=T0)
    newer = RefundFactory.create(failed=True, payment=payment, requested_at=T0 + timedelta(minutes=5))
    tie = RefundFactory.create(payment=payment, requested_at=T0 + timedelta(minutes=5))
    assert list(Refund.objects.all()) == [tie, newer, older]
    assert list(payment.refunds.all()) == [tie, newer, older]
    with pytest.raises(ProtectedError):
        payment.delete()
    actor = StaffUserFactory.create()
    manual = RefundFactory.create(requested_by=actor, origin=Refund.Origin.MANUAL)
    with pytest.raises(ProtectedError):
        actor.delete()
    assert Refund.objects.filter(pk=manual.pk, requested_by=actor).exists()
    assert list(actor.refunds_requested.all()) == [manual]


def test_refund_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("payments_refund")
    assert indexes["refund_one_open_per_payment"].startswith("CREATE UNIQUE INDEX")
    assert "(payment_id) WHERE" in indexes["refund_one_open_per_payment"]
    for state in OPEN_REFUND_STATES:
        assert f"'{state}'" in indexes["refund_one_open_per_payment"]
    assert "'succeeded'" not in indexes["refund_one_open_per_payment"]
    assert "(payment_id, id)" in indexes["refund_payment_idx"]
    assert "(state) WHERE" in indexes["refund_attention_idx"]
    for state in ATTENTION_REFUND_STATES:
        assert f"'{state}'" in indexes["refund_attention_idx"]
    checks = check_clauses("payments_refund")
    assert checks["refund_amount_positive"] == "CHECK ((amount > 0))"
    assert "completed_at IS NOT NULL" in checks["refund_completed_at"]
    assert "completed_at IS NULL" in checks["refund_completed_at"]
    assert "submitted_at IS NOT NULL" in checks["refund_submitted_at"]
    assert {"refund_amount_positive", "refund_completed_at", "refund_submitted_at"} <= set(checks)
    assert "payments_refund_attempts_check" in checks  # PositiveIntegerField: attempts >= 0
    violates("payments_refund_attempts_check", RefundFactory.create, attempts=-1)
