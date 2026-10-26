"""Transactional writes of `payments` (design 02 §2.1 Payment, §2.2 Refund state machine).

Scalar in, values out. ``payments`` sits below ``commerce``: it never touches an order row, never writes an
``OrderEvent`` and never calls ``commerce``; the orchestrator (``commerce.services`` / ``commerce.tasks``)
holds the Order lock, calls these services (which lock the **Payment** row, ``select_for_update(of=
("self",))``) and records the order events. Every refund transition is audited ``refund.state_changed``
(``{refund_id, from, to, attempts, error_code}``; actor = the staff member who resolved it, else system); a
new refund is audited ``refund.requested``.

Refund states (contract §3.1 ``RefundState``): ``— → requested`` (Click; outbox ``refund.submit``) / ``—
→ manual_required`` (Payme cabinet) · ``requested → submitting`` · ``requested → failed`` (dead letter) ·
``submitting → succeeded | failed | unknown`` · ``submitting → submitting`` (redelivery) · ``manual_required
→ succeeded | failed`` and ``unknown → succeeded | failed`` (provider confirmation or staff resolve) ·
``failed`` / ``succeeded`` terminal (a retry is a new row, ``origin = manual``). The payment turns
``refunded`` when the succeeded refunds cover its amount.

Claim fencing (``refund.submit`` is delivered at least once, and an expired outbox lease hands a message to a
second worker while the first may still be talking to Click): :func:`mark_submitting` is a compare-and-set
under the Payment lock on ``(state, attempts)`` and re-claims a ``submitting`` refund only when its last
attempt is older than the outbox lease; :func:`mark_failed` / :func:`mark_unknown` take the attempt number
the worker claimed and do nothing when a newer attempt owns the row. A stale claim raises
:class:`StaleRefundClaim` (the worker stops quietly).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from glowderma.accounts.models import StaffUser
from glowderma.core import audit, enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.payments.models import LATE_CAPTURE, OPEN_REFUND_STATES, Payment, Refund
from glowderma.payments.topics import REFUND_SUBMIT, refund_submit_key

PAYME_STATE_CREATED = 1  # Payme transaction in state 1 is never failed unilaterally (02 P-2)


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


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and timezone.is_naive(value):
        raise ValueError(f"{name} must be timezone-aware")


def _staff(staff_id: int | None) -> StaffUser | None:
    return None if staff_id is None else StaffUser.objects.get(pk=staff_id)


def _lock_payment(payment_id: int) -> Payment:
    payment = Payment.objects.select_for_update(of=("self",)).filter(pk=payment_id).first()
    if payment is None:
        raise PaymentNotFound(f"Payment {payment_id} does not exist")
    return payment


def _payment_result(payment: Payment, *, changed: bool) -> PaymentResult:
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


def _refund_result(
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
    )


# --- payments -----------------------------------------------------------------------------------------------


def create_payment(order_id: int, provider: str, amount: int) -> PaymentResult:
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
    return _payment_result(payment, changed=True)


def confirm_payment(
    payment_id: int, provider_txn_id: str, at: datetime, raw: dict[str, Any] | None = None
) -> PaymentResult:
    """``pending → paid`` (provider capture): ``provider_txn_id``, ``confirmed_at``; idempotent for the same
    transaction id. ``raw`` holds structured protocol fields merged into ``provider_state`` (never a raw
    payload, P-1). Only flips the payment: ``commerce.services.mark_paid`` orchestrates the order effects."""
    _aware(at, "at")
    if not provider_txn_id:
        raise ValueError("A paid payment needs the provider transaction id")
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        if payment.status == enums.PaymentStatus.PAID and payment.provider_txn_id == provider_txn_id:
            return _payment_result(payment, changed=False)
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        payment.status = enums.PaymentStatus.PAID
        payment.provider_txn_id = provider_txn_id
        payment.confirmed_at = at
        if raw:
            payment.provider_state = {**payment.provider_state, **raw}
        payment.save()
        return _payment_result(payment, changed=True)


def can_fail_unilaterally(provider: str, provider_state: dict[str, Any]) -> bool:
    """False for a Payme transaction in state 1: only Payme's ``CancelTransaction`` fails it (02 P-2)."""
    return not (
        provider == enums.PaymentProvider.PAYME and provider_state.get("state") == PAYME_STATE_CREATED
    )


def fail_payment(
    payment_id: int,
    error_code: str,
    at: datetime | None = None,
    *,
    message: str = "",
    unilateral: bool = False,
) -> PaymentResult:
    """``pending → failed`` (``failed_at``, ``error_code``); an already failed payment is a no-op.

    ``unilateral=True`` (reservation expiry, admin cancel, provider switch) leaves a Payme state-1
    transaction ``pending`` (``changed=False``). No order event is written here or by the callers of those
    paths (03 C-8); ``payment_failed`` is reserved for provider callbacks.
    """
    _aware(at, "at")
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        if payment.status == enums.PaymentStatus.FAILED:
            return _payment_result(payment, changed=False)
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        if unilateral and not can_fail_unilaterally(payment.provider, payment.provider_state):
            return _payment_result(payment, changed=False)
        payment.status = enums.PaymentStatus.FAILED
        payment.failed_at = at or timeutils.now()
        payment.error_code = error_code[:32]
        payment.error_message = message[:254]
        payment.save()
        return _payment_result(payment, changed=True)


def attach_transaction(
    payment_id: int, provider_txn_id: str, provider_state: dict[str, Any]
) -> PaymentResult:
    """Bind a provider transaction to a ``pending`` payment (Click Prepare, Payme ``CreateTransaction``).

    ``provider_state`` is merged into the stored protocol fields. The same transaction id again is a merge
    (``changed`` False when nothing changed); a **different** non-empty id already on the payment, or the
    id held by another payment of the provider, raises :class:`TransactionTaken`; a payment that is not
    ``pending`` raises ``InvalidTransition``. The caller holds the Order lock; the Payment row is locked here.
    """
    if not provider_txn_id:
        raise ValueError("A provider transaction id is required")
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        if payment.provider_txn_id and payment.provider_txn_id != provider_txn_id:
            raise TransactionTaken(f"Payment {payment_id} is bound to another transaction")
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        merged = {**payment.provider_state, **provider_state}
        if payment.provider_txn_id == provider_txn_id and merged == payment.provider_state:
            return _payment_result(payment, changed=False)
        payment.provider_txn_id = provider_txn_id
        payment.provider_state = merged
        try:
            with transaction.atomic():
                payment.save(update_fields=["provider_txn_id", "provider_state", "updated_at"])
        except IntegrityError as exc:
            if "payment_provider_txn_unique" in str(exc):
                raise TransactionTaken(f"Transaction {provider_txn_id} belongs to another payment") from exc
            raise
        return _payment_result(payment, changed=True)


def merge_provider_state(payment_id: int, patch: dict[str, Any]) -> PaymentResult:
    """Merge structured protocol fields into ``Payment.provider_state`` (any status; never a raw payload)."""
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        merged = {**payment.provider_state, **patch}
        if merged == payment.provider_state:
            return _payment_result(payment, changed=False)
        payment.provider_state = merged
        payment.save(update_fields=["provider_state", "updated_at"])
        return _payment_result(payment, changed=True)


def mark_late_captured(
    payment_id: int, provider_txn_id: str, at: datetime, raw: dict[str, Any] | None = None
) -> PaymentResult:
    """A refused capture (customer contract §2.13.0): the provider took the money for a payment we do not
    honour. ``failed`` (failed by us) or ``pending`` → ``paid`` with ``error_code = "late_capture"``,
    ``confirmed_at = at``, ``failed_at = None``; the **only** transition out of ``failed``. A payment that is
    already a late capture of the same transaction is a no-op; any other state raises ``InvalidTransition``.
    The late capture never counts as the order's active payment (``payment_one_active_per_order``)."""
    _aware(at, "at")
    if not provider_txn_id:
        raise ValueError("A late capture needs the provider transaction id")
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        if payment.error_code == LATE_CAPTURE and payment.provider_txn_id == provider_txn_id:
            if payment.status in (enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED):
                return _payment_result(payment, changed=False)
        if payment.status not in (enums.PaymentStatus.FAILED, enums.PaymentStatus.PENDING):
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not failed or pending")
        if payment.provider_txn_id and payment.provider_txn_id != provider_txn_id:
            raise TransactionTaken(f"Payment {payment_id} is bound to another transaction")
        payment.status = enums.PaymentStatus.PAID
        payment.provider_txn_id = provider_txn_id
        payment.confirmed_at = at
        payment.failed_at = None
        payment.error_code = LATE_CAPTURE
        payment.error_message = ""
        if raw:
            payment.provider_state = {**payment.provider_state, **raw}
        payment.save()
        return _payment_result(payment, changed=True)


# --- refunds ------------------------------------------------------------------------------------------------


def _audit_state(
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


def _new_refund(
    payment: Payment,
    *,
    amount: int,
    origin: str,
    requested_by: StaffUser | None,
    at: datetime,
    request: Any,
    manual: bool = False,
) -> Refund:
    manual = manual or payment.provider == enums.PaymentProvider.PAYME
    refund = Refund.objects.create(
        payment=payment,
        amount=amount,
        origin=origin,
        requested_by=requested_by,
        requested_at=at,
        state=enums.RefundState.MANUAL_REQUIRED if manual else enums.RefundState.REQUESTED,
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
    manual: bool = False,
) -> RefundResult:
    """Return a paid payment: Click ``requested`` (+ outbox ``refund.submit``), Payme ``manual_required``.

    ``manual=True`` creates a Click refund ``manual_required`` too, without the outbox message (customer
    contract §2.13.0: no ``click_paydoc_id``, or the Click port is fake under the production settings).
    M1 refunds are full (``amount == payment.amount``, O2). A payment with an open refund returns that one
    (``changed=False``), so a replayed cancel never asks twice. ``payment.status`` stays ``paid`` (O5).
    """
    _aware(at, "at")
    if origin not in Refund.Origin.values:
        raise ValueError(f"Unknown refund origin {origin!r}")
    staff = _staff(requested_by)
    with transaction.atomic():
        payment = _lock_payment(payment_id)
        open_refund = payment.refunds.filter(state__in=list(OPEN_REFUND_STATES)).first()
        if open_refund is not None:
            return _refund_result(open_refund, payment, changed=False)
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
            manual=manual,
        )
        return _refund_result(refund, payment, changed=True)


def retry_refund(
    refund_id: int,
    requested_by: int | None,
    *,
    at: datetime | None = None,
    request: Any = None,
    manual: bool = False,
) -> RefundResult:
    """A ``failed`` refund is terminal: a retry is a **new** refund (``origin = manual``) of the payment."""
    _aware(at, "at")
    staff = _staff(requested_by)
    with transaction.atomic():
        payment_id = _payment_id_of(refund_id)
        payment = _lock_payment(payment_id)
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
            manual=manual,
        )
        return _refund_result(new, payment, changed=True)


def _payment_id_of(refund_id: int) -> int:
    payment_id = Refund.objects.filter(pk=refund_id).values_list("payment_id", flat=True).first()
    if payment_id is None:
        raise RefundNotFound(f"Refund {refund_id} does not exist")
    return int(payment_id)


def _locked_refund(refund_id: int) -> tuple[Payment, Refund]:
    payment = _lock_payment(_payment_id_of(refund_id))
    return payment, Refund.objects.get(pk=refund_id)


def mark_submitting(
    refund_id: int,
    at: datetime | None = None,
    *,
    expected_state: str = enums.RefundState.REQUESTED,
    expected_attempts: int | None = None,
    resubmit_after: timedelta | None = None,
) -> RefundResult:
    """The handler claims the refund before calling the provider (committed first): ``submitting``,
    ``submitted_at`` (first time), ``last_attempt_at``, ``attempts += 1``.

    A compare-and-set under the Payment lock: the row must still be in ``expected_state`` (and have
    ``expected_attempts`` attempts when given), else :class:`StaleRefundClaim`. Re-claiming a ``submitting``
    row (``expected_state="submitting"``, a redelivery that is about to submit again after its status check
    said "not reversed") additionally needs the last attempt to be older than ``resubmit_after`` (the outbox
    lease): a younger claim belongs to a worker that may still be waiting for the provider.
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    if expected_state not in (enums.RefundState.REQUESTED, enums.RefundState.SUBMITTING):
        raise ValueError(f"A refund is claimed from requested or submitting, not {expected_state!r}")
    with transaction.atomic():
        payment, refund = _locked_refund(refund_id)
        if refund.state != expected_state or (
            expected_attempts is not None and refund.attempts != expected_attempts
        ):
            raise StaleRefundClaim(f"Refund {refund_id} is {refund.state} (attempt {refund.attempts})")
        if refund.state == enums.RefundState.SUBMITTING:
            last = refund.last_attempt_at
            if resubmit_after is None or (last is not None and last > instant - resubmit_after):
                raise StaleRefundClaim(f"Refund {refund_id} was claimed at {last}; its worker may be alive")
        before = refund.state
        refund.state = enums.RefundState.SUBMITTING
        refund.submitted_at = refund.submitted_at or instant
        refund.last_attempt_at = instant
        refund.attempts += 1
        refund.save()
        _audit_state(refund, before)
        return _refund_result(refund, payment, changed=True)


def _stale(refund: Refund, expected_state: str | None, expected_attempts: int | None) -> bool:
    """The caller's claim no longer owns the row (fencing of :func:`mark_failed` / :func:`mark_unknown`)."""
    if expected_state is not None and refund.state != expected_state:
        return True
    return expected_attempts is not None and (
        refund.state != enums.RefundState.SUBMITTING or refund.attempts != expected_attempts
    )


def _covered(payment: Payment) -> bool:
    succeeded = payment.refunds.filter(state=enums.RefundState.SUCCEEDED).aggregate(total=Sum("amount"))
    return int(succeeded["total"] or 0) >= payment.amount


def mark_succeeded(
    refund_id: int,
    at: datetime | None = None,
    *,
    provider_refund_id: str = "",
    provider_state: dict[str, Any] | None = None,
    actor_id: int | None = None,
    request: Any = None,
    from_failed: bool = False,
    audit_extra: dict[str, Any] | None = None,
) -> RefundResult:
    """The provider returned the money: ``succeeded`` (``completed_at``); the payment becomes ``refunded``
    when the succeeded refunds cover its amount. From ``submitting``, ``manual_required`` (Payme
    ``CancelTransaction`` / staff) or ``unknown`` (reconciliation / staff); a succeeded refund is a no-op.
    ``from_failed=True`` also accepts a ``failed`` refund — only the guarded staff resolve of a failed Click
    refund passes it (``commerce.services.resolve_refund``, customer contract §2.13.0); ``audit_extra`` keys
    are added to the ``refund.state_changed`` audit (``manual_payout``)."""
    _aware(at, "at")
    instant = at or timeutils.now()
    actor = _staff(actor_id)
    with transaction.atomic():
        payment, refund = _locked_refund(refund_id)
        if refund.state == enums.RefundState.SUCCEEDED:
            return _refund_result(refund, payment, changed=False)
        allowed: tuple[str, ...] = (
            enums.RefundState.SUBMITTING,
            enums.RefundState.MANUAL_REQUIRED,
            enums.RefundState.UNKNOWN,
        )
        if from_failed:
            allowed = (*allowed, enums.RefundState.FAILED)
        if refund.state not in allowed:
            raise InvalidTransition(f"Refund {refund_id} is {refund.state}")
        before = refund.state
        refund.state = enums.RefundState.SUCCEEDED
        refund.completed_at = instant
        refund.last_attempt_at = refund.last_attempt_at or instant
        if provider_refund_id:
            refund.provider_refund_id = provider_refund_id[:64]
        if provider_state:
            refund.provider_state = {**refund.provider_state, **provider_state}
        refund.save()
        _audit_state(refund, before, actor=actor, request=request, extra=audit_extra)
        refunded = False
        if payment.status == enums.PaymentStatus.PAID and _covered(payment):
            payment.status = enums.PaymentStatus.REFUNDED
            payment.refunded_at = instant
            payment.save(update_fields=["status", "refunded_at"])
            refunded = True
        return _refund_result(refund, payment, changed=True, payment_refunded=refunded)


def mark_failed(
    refund_id: int,
    error_code: str,
    last_error: str = "",
    at: datetime | None = None,
    *,
    actor_id: int | None = None,
    request: Any = None,
    expected_state: str | None = None,
    expected_attempts: int | None = None,
) -> RefundResult:
    """A definitive failure: ``failed`` (``error_code``, ``last_error``). From ``requested`` (the dead-letter
    hook: never ``unknown``, 03 §5), ``submitting`` (provider refusal), ``unknown`` or ``manual_required``
    (staff resolve, contract §7 item 18). A failed refund is a no-op; ``refunds_needing_action`` counts it.

    Fencing: with ``expected_state`` (the dead-letter hook: ``requested``) or ``expected_attempts`` (the
    handler's claimed attempt, which must still be the newest ``submitting`` one) a row that moved on is left
    alone (``changed=False``)."""
    _aware(at, "at")
    instant = at or timeutils.now()
    actor = _staff(actor_id)
    with transaction.atomic():
        payment, refund = _locked_refund(refund_id)
        if _stale(refund, expected_state, expected_attempts):
            return _refund_result(refund, payment, changed=False)
        if refund.state == enums.RefundState.FAILED:
            return _refund_result(refund, payment, changed=False)
        if refund.state == enums.RefundState.SUCCEEDED:
            raise InvalidTransition(f"Refund {refund_id} already succeeded")
        before = refund.state
        refund.state = enums.RefundState.FAILED
        refund.error_code = error_code[:32]
        refund.last_error = last_error
        refund.last_attempt_at = instant
        refund.save()
        _audit_state(refund, before, actor=actor, request=request)
        return _refund_result(refund, payment, changed=True)


def mark_manual_required(refund_id: int, at: datetime | None = None) -> RefundResult:
    """``requested → manual_required``: a Click refund that must never reach the provider (customer contract
    §2.13.0: its payment has no ``click_paydoc_id``, or the Click port is fake under the production settings)
    — the ``refund.submit`` handler's re-check before any port call. Any other state is left alone
    (``changed=False``)."""
    _aware(at, "at")
    with transaction.atomic():
        payment, refund = _locked_refund(refund_id)
        if refund.state != enums.RefundState.REQUESTED:
            return _refund_result(refund, payment, changed=False)
        before = refund.state
        refund.state = enums.RefundState.MANUAL_REQUIRED
        refund.last_attempt_at = at or timeutils.now()
        refund.save()
        _audit_state(refund, before)
        return _refund_result(refund, payment, changed=True)


def mark_unknown(
    refund_id: int,
    last_error: str = "",
    at: datetime | None = None,
    *,
    expected_attempts: int | None = None,
) -> RefundResult:
    """Transport failure after submission and the follow-up status check failed too: ``unknown``. Never
    retried automatically (02 §2.2); staff resolve it. With ``expected_attempts`` a row whose newest attempt
    is not the caller's is left alone (``changed=False``)."""
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        payment, refund = _locked_refund(refund_id)
        if _stale(refund, None, expected_attempts):
            return _refund_result(refund, payment, changed=False)
        if refund.state == enums.RefundState.UNKNOWN:
            return _refund_result(refund, payment, changed=False)
        if refund.state != enums.RefundState.SUBMITTING:
            raise InvalidTransition(f"Refund {refund_id} is {refund.state}, not submitting")
        before = refund.state
        refund.state = enums.RefundState.UNKNOWN
        refund.last_error = last_error
        refund.last_attempt_at = instant
        refund.save()
        _audit_state(refund, before)
        return _refund_result(refund, payment, changed=True)


__all__ = [
    "PaymentError",
    "PaymentNotFound",
    "PaymentResult",
    "RefundNotFound",
    "RefundResult",
    "StaleRefundClaim",
    "TransactionTaken",
    "attach_transaction",
    "can_fail_unilaterally",
    "confirm_payment",
    "create_payment",
    "create_refund",
    "fail_payment",
    "mark_failed",
    "mark_late_captured",
    "mark_manual_required",
    "mark_submitting",
    "mark_succeeded",
    "mark_unknown",
    "merge_provider_state",
    "retry_refund",
]
