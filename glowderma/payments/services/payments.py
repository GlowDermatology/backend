"""Payment writes (design 02 §2.1): create, confirm, fail, attach the provider transaction, merge the provider
state and the late capture."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction

from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.payments.models import LATE_CAPTURE, Payment, uzum_session_over
from glowderma.payments.services import common, results

PAYME_STATE_CREATED = 1  # Payme transaction in state 1 is never failed unilaterally (02 P-2)


def create_payment(order_id: int, provider: str, amount: int) -> results.PaymentResult:
    """A ``pending`` attempt to collect ``amount`` (= the order total) with ``provider``.

    At most one ``pending``/``paid`` payment per order (partial unique): a second active one raises
    ``Conflict``. The caller (checkout) holds the Order lock.
    """
    if provider not in enums.PaymentProvider.values:
        raise ValueError(f"Unknown payment provider {provider!r}")
    if amount <= 0:
        raise ValueError("A payment collects a positive amount")
    try:
        with transaction.atomic():
            payment = Payment.objects.create(order_id=order_id, provider=provider, amount=amount)
    except IntegrityError as exc:
        if "payment_one_active_per_order" in str(exc):
            raise Conflict(f"Order {order_id} already has an active payment") from exc
        raise
    return results.payment_result(payment, changed=True)


def confirm_payment(
    payment_id: int, provider_txn_id: str, at: datetime, raw: dict[str, Any] | None = None
) -> results.PaymentResult:
    """``pending → paid`` (provider capture): ``provider_txn_id``, ``confirmed_at``; idempotent for the same
    transaction id. ``raw`` holds structured protocol fields merged into ``provider_state`` (never a raw
    payload, P-1). Only flips the payment: ``commerce.services.mark_paid`` orchestrates the order effects."""
    common.require_aware(at, "at")
    if not provider_txn_id:
        raise ValueError("A paid payment needs the provider transaction id")
    with transaction.atomic():
        payment = common.lock_payment(payment_id)
        if payment.status == enums.PaymentStatus.PAID and payment.provider_txn_id == provider_txn_id:
            return results.payment_result(payment, changed=False)
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        payment.status = enums.PaymentStatus.PAID
        payment.provider_txn_id = provider_txn_id
        payment.confirmed_at = at
        if raw:
            payment.provider_state = {**payment.provider_state, **raw}
        payment.save()
        return results.payment_result(payment, changed=True)


def can_fail_unilaterally(provider: str, provider_state: dict[str, Any], at: datetime | None = None) -> bool:
    """False for a Payme transaction in state 1 (only Payme's ``CancelTransaction`` fails it, 02 P-2) and
    for an Uzum payment whose hosted form may still take the money (its session is not over: Uzum's status
    decides it, ``commerce.uzum_flow``). Click is always failable (its Complete is checked against us)."""
    if provider == enums.PaymentProvider.PAYME:
        return provider_state.get("state") != PAYME_STATE_CREATED
    if provider == enums.PaymentProvider.UZUM:
        return uzum_session_over(provider_state, at or timeutils.now())
    if provider == enums.PaymentProvider.CLICK:
        return True
    raise ValueError(f"Unknown payment provider {provider!r}")


def fail_payment(
    payment_id: int,
    error_code: str,
    at: datetime | None = None,
    *,
    message: str = "",
    unilateral: bool = False,
) -> results.PaymentResult:
    """``pending → failed`` (``failed_at``, ``error_code``); an already failed payment is a no-op.

    ``unilateral=True`` (reservation expiry, admin cancel, provider switch) leaves a Payme state-1
    transaction, and an Uzum payment whose form may still be paid, ``pending`` (``changed=False``). No order
    event is written here or by the callers of those paths (03 C-8); ``payment_failed`` is reserved for
    provider callbacks.
    """
    common.require_aware(at, "at")
    with transaction.atomic():
        payment = common.lock_payment(payment_id)
        if payment.status == enums.PaymentStatus.FAILED:
            return results.payment_result(payment, changed=False)
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        if unilateral and not can_fail_unilaterally(payment.provider, payment.provider_state, at):
            return results.payment_result(payment, changed=False)
        payment.status = enums.PaymentStatus.FAILED
        payment.failed_at = at or timeutils.now()
        payment.error_code = error_code[:32]
        payment.error_message = message[:254]
        payment.save()
        return results.payment_result(payment, changed=True)


def attach_transaction(
    payment_id: int, provider_txn_id: str, provider_state: dict[str, Any]
) -> results.PaymentResult:
    """Bind a provider transaction to a ``pending`` payment (Click Prepare, Payme ``CreateTransaction``).

    ``provider_state`` is merged into the stored protocol fields. The same transaction id again is a merge
    (``changed`` False when nothing changed); a **different** non-empty id already on the payment, or the
    id held by another payment of the provider, raises :class:`TransactionTaken`; a payment that is not
    ``pending`` raises ``InvalidTransition``. The caller holds the Order lock; the Payment row is locked here.
    """
    if not provider_txn_id:
        raise ValueError("A provider transaction id is required")
    with transaction.atomic():
        payment = common.lock_payment(payment_id)
        if payment.provider_txn_id and payment.provider_txn_id != provider_txn_id:
            raise results.TransactionTaken(f"Payment {payment_id} is bound to another transaction")
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        merged = {**payment.provider_state, **provider_state}
        if payment.provider_txn_id == provider_txn_id and merged == payment.provider_state:
            return results.payment_result(payment, changed=False)
        payment.provider_txn_id = provider_txn_id
        payment.provider_state = merged
        try:
            with transaction.atomic():
                payment.save(update_fields=["provider_txn_id", "provider_state", "updated_at"])
        except IntegrityError as exc:
            if "payment_provider_txn_unique" in str(exc):
                raise results.TransactionTaken(
                    f"Transaction {provider_txn_id} belongs to another payment"
                ) from exc
            raise
        return results.payment_result(payment, changed=True)


def merge_provider_state(payment_id: int, patch: dict[str, Any]) -> results.PaymentResult:
    """Merge structured protocol fields into ``Payment.provider_state`` (any status; never a raw payload)."""
    with transaction.atomic():
        payment = common.lock_payment(payment_id)
        merged = {**payment.provider_state, **patch}
        if merged == payment.provider_state:
            return results.payment_result(payment, changed=False)
        payment.provider_state = merged
        payment.save(update_fields=["provider_state", "updated_at"])
        return results.payment_result(payment, changed=True)


def mark_late_captured(
    payment_id: int, provider_txn_id: str, at: datetime, raw: dict[str, Any] | None = None
) -> results.PaymentResult:
    """A refused capture (customer contract §2.13.0): the provider took the money for a payment we do not
    honour. ``failed`` (failed by us) or ``pending`` → ``paid`` with ``error_code = "late_capture"``,
    ``confirmed_at = at``, ``failed_at = None``; the **only** transition out of ``failed``. A payment that is
    already a late capture of the same transaction is a no-op; any other state raises ``InvalidTransition``.
    The late capture never counts as the order's active payment (``payment_one_active_per_order``)."""
    common.require_aware(at, "at")
    if not provider_txn_id:
        raise ValueError("A late capture needs the provider transaction id")
    with transaction.atomic():
        payment = common.lock_payment(payment_id)
        if payment.error_code == LATE_CAPTURE and payment.provider_txn_id == provider_txn_id:
            if payment.status in (enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED):
                return results.payment_result(payment, changed=False)
        if payment.status not in (enums.PaymentStatus.FAILED, enums.PaymentStatus.PENDING):
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not failed or pending")
        if payment.provider_txn_id and payment.provider_txn_id != provider_txn_id:
            raise results.TransactionTaken(f"Payment {payment_id} is bound to another transaction")
        payment.status = enums.PaymentStatus.PAID
        payment.provider_txn_id = provider_txn_id
        payment.confirmed_at = at
        payment.failed_at = None
        payment.error_code = LATE_CAPTURE
        payment.error_message = ""
        if raw:
            payment.provider_state = {**payment.provider_state, **raw}
        payment.save()
        return results.payment_result(payment, changed=True)


__all__ = [
    "PAYME_STATE_CREATED",
    "attach_transaction",
    "can_fail_unilaterally",
    "confirm_payment",
    "create_payment",
    "fail_payment",
    "mark_late_captured",
    "merge_provider_state",
]
