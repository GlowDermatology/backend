"""What the payment services answer and raise: the results of a payment or refund write and the errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from glowderma.payments.models import Payment, Refund


class PaymentError(Exception):
    """Base class of the module's typed errors."""


class PaymentNotFound(PaymentError):
    pass


class RefundNotFound(PaymentError):
    pass


class StaleRefundClaim(PaymentError):
    """The refund moved on since the worker read it (another worker claimed it, or it was resolved)."""


class TransactionTaken(PaymentError):
    """The payment already carries a different provider transaction id (customer contract §2.13.0 step 4), or
    another payment of the provider holds this one (``payment_provider_txn_unique``)."""


@dataclass(frozen=True, slots=True)
class PaymentResult:
    payment_id: int
    order_id: int
    provider: str
    status: str
    amount: int
    provider_txn_id: str
    confirmed_at: datetime | None
    changed: bool  # False = idempotent no-op


@dataclass(frozen=True, slots=True)
class RefundResult:
    refund_id: int
    payment_id: int
    order_id: int
    provider: str
    provider_txn_id: str
    state: str
    amount: int
    attempts: int
    error_code: str
    changed: bool  # False = idempotent no-op (already in the target state / an open refund returned)
    payment_refunded: bool = False  # this call turned the payment ``refunded``
    manual_reason: str = ""  # RefundManualReason when the refund is (or was) manual_required, else ""


def payment_result(payment: Payment, *, changed: bool) -> PaymentResult:
    return PaymentResult(
        payment_id=payment.pk,
        order_id=payment.order_id,
        provider=payment.provider,
        status=payment.status,
        amount=payment.amount,
        provider_txn_id=payment.provider_txn_id,
        confirmed_at=payment.confirmed_at,
        changed=changed,
    )


def refund_result(
    refund: Refund, payment: Payment, *, changed: bool, payment_refunded: bool = False
) -> RefundResult:
    return RefundResult(
        refund_id=refund.pk,
        payment_id=payment.pk,
        order_id=payment.order_id,
        provider=payment.provider,
        provider_txn_id=payment.provider_txn_id,
        state=refund.state,
        amount=refund.amount,
        attempts=refund.attempts,
        error_code=refund.error_code,
        changed=changed,
        payment_refunded=payment_refunded,
        manual_reason=refund.manual_reason,
    )


__all__ = [
    "PaymentError",
    "PaymentNotFound",
    "PaymentResult",
    "RefundNotFound",
    "RefundResult",
    "StaleRefundClaim",
    "TransactionTaken",
    "payment_result",
    "refund_result",
]
