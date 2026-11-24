"""The claim-fenced refund submission (design 02 §2.2): claim, release, and the outcomes — succeeded, failed,
manual required or unknown."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Sum

from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import InvalidTransition
from glowderma.payments.models import Payment, Refund
from glowderma.payments.services import common, results


def mark_submitting(
    refund_id: int,
    at: datetime | None = None,
    *,
    expected_state: str = enums.RefundState.REQUESTED,
    expected_attempts: int | None = None,
    resubmit_after: timedelta | None = None,
) -> results.RefundResult:
    """The handler claims the refund before calling the provider (committed first): ``submitting``,
    ``submitted_at`` (first time), ``last_attempt_at``, ``attempts += 1``.

    A compare-and-set under the Payment lock: the row must still be in ``expected_state`` (and have
    ``expected_attempts`` attempts when given), else :class:`StaleRefundClaim`. Re-claiming a ``submitting``
    row (``expected_state="submitting"``, a redelivery that is about to submit again after its status check
    said "not reversed") additionally needs the last attempt to be older than ``resubmit_after`` (the outbox
    lease): a younger claim belongs to a worker that may still be waiting for the provider.
    """
    common.require_aware(at, "at")
    instant = at or timeutils.now()
    if expected_state not in (enums.RefundState.REQUESTED, enums.RefundState.SUBMITTING):
        raise ValueError(f"A refund is claimed from requested or submitting, not {expected_state!r}")
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        if refund.state != expected_state or (
            expected_attempts is not None and refund.attempts != expected_attempts
        ):
            raise results.StaleRefundClaim(
                f"Refund {refund_id} is {refund.state} (attempt {refund.attempts})"
            )
        if refund.state == enums.RefundState.SUBMITTING:
            last = refund.last_attempt_at
            if resubmit_after is None or (last is not None and last > instant - resubmit_after):
                raise results.StaleRefundClaim(
                    f"Refund {refund_id} was claimed at {last}; its worker may be alive"
                )
        before = refund.state
        refund.state = enums.RefundState.SUBMITTING
        refund.submitted_at = refund.submitted_at or instant
        refund.last_attempt_at = instant
        refund.attempts += 1
        refund.save()
        common.audit_refund_state(refund, before)
        return results.refund_result(refund, payment, changed=True)


def release_claim(
    refund_id: int, last_error: str = "", at: datetime | None = None, *, expected_attempts: int
) -> results.RefundResult:
    """``submitting → requested``: the provider answered that it did **not** process the claimed attempt (a
    redirect, 408 / 425 / 429, an authentication refusal; M3 Click reversal), so nothing was sent and the
    outbox may submit again after its status check. Fenced like :func:`mark_unknown`: only the worker whose
    attempt is still the newest ``submitting`` one releases it (``changed=False`` otherwise)."""
    common.require_aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        if _stale(refund, enums.RefundState.SUBMITTING, expected_attempts):
            return results.refund_result(refund, payment, changed=False)
        before = refund.state
        refund.state = enums.RefundState.REQUESTED
        refund.last_error = last_error
        refund.last_attempt_at = instant
        refund.save()
        common.audit_refund_state(refund, before)
        return results.refund_result(refund, payment, changed=True)


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
) -> results.RefundResult:
    """The provider returned the money: ``succeeded`` (``completed_at``); the payment becomes ``refunded``
    when the succeeded refunds cover its amount. From ``submitting``, ``manual_required`` (Payme
    ``CancelTransaction`` / staff) or ``unknown`` (reconciliation / staff); a succeeded refund is a no-op.
    ``from_failed=True`` also accepts a ``failed`` refund — only the guarded staff resolve of a failed Click
    refund passes it (``commerce.services.resolve_refund``, customer contract §2.13.0); ``audit_extra`` keys
    are added to the ``refund.state_changed`` audit (``manual_payout``)."""
    common.require_aware(at, "at")
    instant = at or timeutils.now()
    actor = common.staff_or_none(actor_id)
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        if refund.state == enums.RefundState.SUCCEEDED:
            return results.refund_result(refund, payment, changed=False)
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
        common.audit_refund_state(refund, before, actor=actor, request=request, extra=audit_extra)
        refunded = False
        if payment.status == enums.PaymentStatus.PAID and _covered(payment):
            payment.status = enums.PaymentStatus.REFUNDED
            payment.refunded_at = instant
            payment.save(update_fields=["status", "refunded_at"])
            refunded = True
        return results.refund_result(refund, payment, changed=True, payment_refunded=refunded)


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
) -> results.RefundResult:
    """A definitive failure: ``failed`` (``error_code``, ``last_error``). From ``requested`` (the dead-letter
    hook: never ``unknown``, 03 §5), ``submitting`` (provider refusal), ``unknown`` or ``manual_required``
    (staff resolve, contract §7 item 18). A failed refund is a no-op; ``refunds_needing_action`` counts it.

    Fencing: with ``expected_state`` (the dead-letter hook: ``requested``) or ``expected_attempts`` (the
    handler's claimed attempt, which must still be the newest ``submitting`` one) a row that moved on is left
    alone (``changed=False``)."""
    common.require_aware(at, "at")
    instant = at or timeutils.now()
    actor = common.staff_or_none(actor_id)
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        if _stale(refund, expected_state, expected_attempts):
            return results.refund_result(refund, payment, changed=False)
        if refund.state == enums.RefundState.FAILED:
            return results.refund_result(refund, payment, changed=False)
        if refund.state == enums.RefundState.SUCCEEDED:
            raise InvalidTransition(f"Refund {refund_id} already succeeded")
        before = refund.state
        refund.state = enums.RefundState.FAILED
        refund.error_code = error_code[:32]
        refund.last_error = last_error
        refund.last_attempt_at = instant
        refund.save()
        common.audit_refund_state(refund, before, actor=actor, request=request)
        return results.refund_result(refund, payment, changed=True)


def merge_refund_state(refund_id: int, patch: dict[str, Any]) -> results.RefundResult:
    """Merge structured protocol fields into ``Refund.provider_state`` (Uzum: our ``X-Operation-Id`` and
    when Uzum accepted it); the state does not change. Never a raw payload."""
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        merged = {**refund.provider_state, **patch}
        if merged == refund.provider_state:
            return results.refund_result(refund, payment, changed=False)
        refund.provider_state = merged
        refund.save(update_fields=["provider_state", "updated_at"])
        return results.refund_result(refund, payment, changed=True)


def mark_manual_required(refund_id: int, at: datetime | None = None, *, reason: str) -> results.RefundResult:
    """``requested → manual_required``: a Click refund that must never reach the provider (customer contract
    §2.13.0: its payment has no ``click_paydoc_id`` — ``reason`` ``no_paydoc`` —, or the Click port is fake
    under the production settings — ``fake_mode``) — the ``refund.submit`` handler's re-check before any port
    call; the reason is stored on the refund. Any other state is left alone (``changed=False``)."""
    common.require_aware(at, "at")
    if reason not in enums.RefundManualReason.values:
        raise ValueError(f"Unknown refund manual reason {reason!r}")
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        if refund.state != enums.RefundState.REQUESTED:
            return results.refund_result(refund, payment, changed=False)
        before = refund.state
        refund.state = enums.RefundState.MANUAL_REQUIRED
        refund.manual_reason = reason
        refund.last_attempt_at = at or timeutils.now()
        refund.save()
        common.audit_refund_state(refund, before, extra={"manual_reason": reason})
        return results.refund_result(refund, payment, changed=True)


def mark_unknown(
    refund_id: int,
    last_error: str = "",
    at: datetime | None = None,
    *,
    expected_attempts: int | None = None,
) -> results.RefundResult:
    """Transport failure after submission and the follow-up status check failed too: ``unknown``. Never
    retried automatically (02 §2.2); staff resolve it. With ``expected_attempts`` a row whose newest attempt
    is not the caller's is left alone (``changed=False``)."""
    common.require_aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        payment, refund = common.locked_refund(refund_id)
        if _stale(refund, None, expected_attempts):
            return results.refund_result(refund, payment, changed=False)
        if refund.state == enums.RefundState.UNKNOWN:
            return results.refund_result(refund, payment, changed=False)
        if refund.state != enums.RefundState.SUBMITTING:
            raise InvalidTransition(f"Refund {refund_id} is {refund.state}, not submitting")
        before = refund.state
        refund.state = enums.RefundState.UNKNOWN
        refund.last_error = last_error
        refund.last_attempt_at = instant
        refund.save()
        common.audit_refund_state(refund, before)
        return results.refund_result(refund, payment, changed=True)


__all__ = [
    "mark_failed",
    "mark_manual_required",
    "mark_submitting",
    "mark_succeeded",
    "mark_unknown",
    "release_claim",
]
