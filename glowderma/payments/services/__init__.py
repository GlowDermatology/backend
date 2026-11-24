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

The package groups them by subject: ``payments`` (create, confirm, fail, attach a provider transaction,
merge provider state, late capture), ``refund_requests`` (a new refund and its retry) and ``refund_attempts``
(the claim-fenced submission and its outcomes); ``results`` holds the answers and errors, ``common`` the
locks. Callers import this facade.
"""

from glowderma.payments.services.payments import (
    attach_transaction,
    can_fail_unilaterally,
    confirm_payment,
    create_payment,
    fail_payment,
    mark_late_captured,
    merge_provider_state,
)
from glowderma.payments.services.refund_attempts import (
    mark_failed,
    mark_manual_required,
    mark_submitting,
    mark_succeeded,
    mark_unknown,
    release_claim,
)
from glowderma.payments.services.refund_requests import (
    create_refund,
    retry_refund,
)
from glowderma.payments.services.results import (
    PaymentError,
    PaymentNotFound,
    PaymentResult,
    RefundNotFound,
    RefundResult,
    StaleRefundClaim,
    TransactionTaken,
)

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
    "release_claim",
    "retry_refund",
]
