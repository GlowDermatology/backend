"""What the payment services share: the aware-time check, the staff actor, the Payment row lock and the refund
state audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

from glowderma.accounts.models import StaffUser
from glowderma.core import audit
from glowderma.payments.models import Payment, Refund
from glowderma.payments.services import results


def require_aware(value: datetime | None, name: str) -> None:
    if value is not None and timezone.is_naive(value):
        raise ValueError(f"{name} must be timezone-aware")


def staff_or_none(staff_id: int | None) -> StaffUser | None:
    return None if staff_id is None else StaffUser.objects.get(pk=staff_id)


def lock_payment(payment_id: int) -> Payment:
    payment = Payment.objects.select_for_update(of=("self",)).filter(pk=payment_id).first()
    if payment is None:
        raise results.PaymentNotFound(f"Payment {payment_id} does not exist")
    return payment


def audit_refund_state(
    refund: Refund,
    before: str,
    *,
    actor: StaffUser | None = None,
    request: Any = None,
    extra: dict[str, Any] | None = None,
) -> None:
    audit.record(
        action="refund.state_changed",
        entity_type="refund",
        entity_id=str(refund.pk),
        actor=actor,
        request=request,
        after={
            "refund_id": refund.pk,
            "payment_id": refund.payment_id,
            "from": before,
            "to": refund.state,
            "attempts": refund.attempts,
            "error_code": refund.error_code or None,
            **(extra or {}),
        },
    )


def payment_id_of(refund_id: int) -> int:
    payment_id = Refund.objects.filter(pk=refund_id).values_list("payment_id", flat=True).first()
    if payment_id is None:
        raise results.RefundNotFound(f"Refund {refund_id} does not exist")
    return int(payment_id)


def locked_refund(refund_id: int) -> tuple[Payment, Refund]:
    payment = lock_payment(payment_id_of(refund_id))
    return payment, Refund.objects.get(pk=refund_id)


__all__ = [
    "locked_refund",
    "payment_id_of",
    "audit_refund_state",
    "lock_payment",
    "require_aware",
    "staff_or_none",
]
