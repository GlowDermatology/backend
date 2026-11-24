"""New refunds (design 02 §2.2): requested (Click; outbox ``refund.submit``) or manual (Payme cabinet), and a
retry as a new row."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction

from glowderma.accounts.models import StaffUser
from glowderma.core import audit, enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.payments.models import OPEN_REFUND_STATES, Payment, Refund
from glowderma.payments.services import common, results
from glowderma.payments.topics import REFUND_SUBMIT, refund_submit_key


def _new_refund(
    payment: Payment,
    *,
    amount: int,
    origin: str,
    requested_by: StaffUser | None,
    at: datetime,
    request: Any,
    manual_reason: str = "",
) -> Refund:
    if payment.provider == enums.PaymentProvider.PAYME:
        manual_reason = enums.RefundManualReason.PAYME
    elif manual_reason and manual_reason not in enums.RefundManualReason.values:
        raise ValueError(f"Unknown refund manual reason {manual_reason!r}")
    manual = bool(manual_reason)
    refund = Refund.objects.create(
        payment=payment,
        amount=amount,
        origin=origin,
        requested_by=requested_by,
        requested_at=at,
        state=enums.RefundState.MANUAL_REQUIRED if manual else enums.RefundState.REQUESTED,
        manual_reason=manual_reason,
    )
    if not manual:
        outbox.enqueue(
            REFUND_SUBMIT,
            {
                "refund_id": refund.pk,
                "payment_id": payment.pk,
                "order_id": payment.order_id,
                "provider_txn_id": payment.provider_txn_id,
                "amount": amount,
            },
            dedupe_key=refund_submit_key(refund.pk),
        )
    audit.record(
        action="refund.requested",
        entity_type="refund",
        entity_id=str(refund.pk),
        actor=requested_by,
        request=request,
        after={
            "refund_id": refund.pk,
            "payment_id": payment.pk,
            "order_id": payment.order_id,
            "provider": payment.provider,
            "amount": amount,
            "state": refund.state,
            "origin": origin,
            "manual_reason": manual_reason or None,
        },
    )
    return refund


def create_refund(
    payment_id: int,
    amount: int,
    requested_by: int | None,
    origin: str = Refund.Origin.ORDER_CANCELLED,
    *,
    at: datetime | None = None,
    request: Any = None,
    manual_reason: str = "",
) -> results.RefundResult:
    """Return a paid payment: Click ``requested`` (+ outbox ``refund.submit``), Payme ``manual_required``
    (``manual_reason`` ``payme``).

    A ``manual_reason`` (:class:`~glowderma.core.enums.RefundManualReason`) creates a Click refund
    ``manual_required`` too, without the outbox message (customer contract §2.13.0: ``no_paydoc`` — no
    ``click_paydoc_id``; ``fake_mode`` — the Click port is fake under the production settings); the reason is
    stored on the refund (admin contract §2.2 ``Refund.manual_reason``).
    M1 refunds are full (``amount == payment.amount``, O2). A payment with an open refund returns that one
    (``changed=False``), so a replayed cancel never asks twice. ``payment.status`` stays ``paid`` (O5).
    """
    common.require_aware(at, "at")
    if origin not in Refund.Origin.values:
        raise ValueError(f"Unknown refund origin {origin!r}")
    staff = common.staff_or_none(requested_by)
    with transaction.atomic():
        payment = common.lock_payment(payment_id)
        open_refund = payment.refunds.filter(state__in=list(OPEN_REFUND_STATES)).first()
        if open_refund is not None:
            return results.refund_result(open_refund, payment, changed=False)
        if payment.status != enums.PaymentStatus.PAID:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not paid")
        if amount != payment.amount:
            raise ValueError("M1 refunds return the full payment amount")
        refund = _new_refund(
            payment,
            amount=amount,
            origin=origin,
            requested_by=staff,
            at=at or timeutils.now(),
            request=request,
            manual_reason=manual_reason,
        )
        return results.refund_result(refund, payment, changed=True)


def retry_refund(
    refund_id: int,
    requested_by: int | None,
    *,
    at: datetime | None = None,
    request: Any = None,
    manual_reason: str = "",
) -> results.RefundResult:
    """A ``failed`` refund is terminal: a retry is a **new** refund (``origin = manual``) of the payment
    (``manual_reason`` as in :func:`create_refund`)."""
    common.require_aware(at, "at")
    staff = common.staff_or_none(requested_by)
    with transaction.atomic():
        payment_id = common.payment_id_of(refund_id)
        payment = common.lock_payment(payment_id)
        refund = Refund.objects.get(pk=refund_id)
        if refund.state != enums.RefundState.FAILED:
            raise InvalidTransition(f"Refund {refund_id} is {refund.state}, not failed")
        if payment.status != enums.PaymentStatus.PAID:
            raise InvalidTransition(f"Payment {payment.pk} is {payment.status}, not paid")
        if payment.refunds.filter(state__in=list(OPEN_REFUND_STATES)).exists():
            raise Conflict(f"Payment {payment.pk} already has an open refund")
        new = _new_refund(
            payment,
            amount=refund.amount,
            origin=Refund.Origin.MANUAL,
            requested_by=staff,
            at=at or timeutils.now(),
            request=request,
            manual_reason=manual_reason,
        )
        return results.refund_result(new, payment, changed=True)


__all__ = [
    "create_refund",
    "retry_refund",
]
