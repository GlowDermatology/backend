"""The frozen rows payments leave the module as, with the contract's field names."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from glowderma.core import enums
from glowderma.payments.models import (
    LATE_CAPTURE,
    Payment,
    Refund,
)


@dataclass(frozen=True, slots=True)
class PaymentRow:
    id: int
    order_id: int
    provider: str
    status: str
    amount: int
    transaction_id: str | None
    created_at: datetime
    confirmed_at: datetime | None
    provider_state: dict[str, Any]
    error_code: str = ""
    failed_at: datetime | None = None

    @property
    def late_capture(self) -> bool:
        return self.error_code == LATE_CAPTURE


@dataclass(frozen=True, slots=True)
class RefundRow:
    """Contract ``Refund`` (+ the internals the handlers need)."""

    id: int
    payment_id: int
    order_id: int
    state: str
    provider: str
    amount: int
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    provider_txn_id: str
    attempts: int
    submitted_at: datetime | None
    error_code: str
    # The provider's payment reference for refund/status calls: Click's ``click_paydoc_id`` (customer contract
    # §2.13.0, never ``click_trans_id``); ``""`` when the payment has none (seeded/legacy rows).
    provider_payment_ref: str = ""
    # Why the refund is (or was) ``manual_required`` (admin contract §2.2 ``Refund.manual_reason``): the
    # stored ``Refund.manual_reason``; for a ``manual_required`` row stored without one (a factory row, a
    # row the M3 backfill could not place) the reason its payment implies; ``None`` otherwise.
    manual_reason: str | None = None


def click_paydoc_id(provider_state: Any) -> str:
    """``provider_state["click_paydoc_id"]`` as text, ``""`` when absent."""
    value = provider_state.get("click_paydoc_id") if isinstance(provider_state, dict) else None
    return "" if value in (None, "") else str(value)


def payment_row(payment: Payment) -> PaymentRow:
    return PaymentRow(
        id=payment.pk,
        order_id=payment.order_id,
        provider=payment.provider,
        status=payment.status,
        amount=payment.amount,
        transaction_id=payment.provider_txn_id or None,
        created_at=payment.created_at,
        confirmed_at=payment.confirmed_at,
        provider_state=dict(payment.provider_state or {}),
        error_code=payment.error_code,
        failed_at=payment.failed_at,
    )


def refund_row(refund: Refund, payment: Payment | None = None) -> RefundRow:
    owner = payment or refund.payment
    return RefundRow(
        id=refund.pk,
        payment_id=owner.pk,
        order_id=owner.order_id,
        state=refund.state,
        provider=owner.provider,
        amount=refund.amount,
        requested_at=refund.requested_at,
        updated_at=refund.updated_at,
        completed_at=refund.completed_at,
        provider_txn_id=owner.provider_txn_id,
        attempts=refund.attempts,
        submitted_at=refund.submitted_at,
        error_code=refund.error_code,
        provider_payment_ref=provider_payment_ref(owner),
        manual_reason=refund_manual_reason_of(refund, owner),
    )


def provider_payment_ref(payment: Payment) -> str:
    """The id a provider's refund call is keyed by: Click's ``click_paydoc_id`` (never ``click_trans_id``),
    Uzum's order id (the payment's provider transaction id); ``""`` for Payme (cabinet refunds)."""
    if payment.provider == enums.PaymentProvider.CLICK:
        return click_paydoc_id(payment.provider_state)
    if payment.provider == enums.PaymentProvider.UZUM:
        return str(payment.provider_txn_id or "")
    return ""


def refund_manual_reason_of(refund: Refund, payment: Payment) -> str | None:
    """``Refund.manual_reason`` for the API (:attr:`RefundRow.manual_reason`). The fallback of a
    ``manual_required`` row without a stored reason never assumes Payme for a Click refund: Payme →
    ``payme``; Click → ``no_paydoc`` without a ``click_paydoc_id``, else ``fake_mode`` (the only other reason
    a Click refund is manual)."""
    if refund.manual_reason:
        return str(refund.manual_reason)
    if refund.state != enums.RefundState.MANUAL_REQUIRED:
        return None
    if payment.provider == enums.PaymentProvider.PAYME:
        return enums.RefundManualReason.PAYME.value
    if payment.provider == enums.PaymentProvider.UZUM:
        return enums.RefundManualReason.UZUM_FAKE.value  # the only reason an Uzum refund is manual
    if not click_paydoc_id(payment.provider_state):
        return enums.RefundManualReason.NO_PAYDOC.value
    return enums.RefundManualReason.FAKE_MODE.value


__all__ = [
    "PaymentRow",
    "RefundRow",
    "click_paydoc_id",
    "payment_row",
    "provider_payment_ref",
    "refund_manual_reason_of",
    "refund_row",
]
