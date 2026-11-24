"""The nightly consistency report's payment checks (``reporting.consistency_report``, docs/ops/monitoring.md).

Each returns a ``core.consistency.Finding`` (count + the first ids, one statement); the comment names the
index that bounds the scan."""

from __future__ import annotations

from datetime import datetime

from django.db.models import (
    Exists,
    OuterRef,
    Q,
)
from django.db.models.fields.json import KeyTextTransform

from glowderma.core import enums
from glowderma.core.consistency import Finding, finding
from glowderma.payments.models import (
    LATE_CAPTURE,
    OPEN_REFUND_STATES,
    Payment,
    Refund,
)
from glowderma.payments.selectors import refund_reads


def stuck_pending_payments(expired_before: datetime, *, limit: int) -> Finding:
    """K2: ``pending`` payments holding a provider transaction whose order's reservation ran out before
    ``expired_before`` (the report passes now − ``PAYMENT_IN_FLIGHT_GRACE_SECONDS`` − its threshold): the
    capture neither completed nor was cancelled. Payme keeps a state-1 transaction open up to its own 12 h
    timeout, so a Payme row can show here until Payme cancels it.

    Plan: ``payment_status_created_idx`` range scan on ``status = 'pending'`` (only the open payments) with
    ``created_at < expired_before`` — implied, because a payment is created before its order's reservation
    runs out — then the order by its primary key."""
    queryset = Payment.objects.filter(
        status=enums.PaymentStatus.PENDING,
        created_at__lt=expired_before,
        order__reservation_expires_at__lt=expired_before,
    ).exclude(provider_txn_id="")
    return finding(queryset, limit=limit)


def click_paid_without_paydoc(confirmed_since: datetime, *, limit: int) -> Finding:
    """K2: Click payments ``paid`` since ``confirmed_since`` without ``provider_state.click_paydoc_id`` —
    their refund cannot be sent to Click (customer contract §2.13.0) and becomes ``manual_required``.

    Plan: ``payment_confirmed_idx`` (partial on paid/refunded) range scan on ``confirmed_at >=
    confirmed_since`` (the report's look-back), filtered by provider and the JSON key."""
    paydoc = KeyTextTransform("click_paydoc_id", "provider_state")
    queryset = (
        Payment.objects.filter(
            provider=enums.PaymentProvider.CLICK,
            status=enums.PaymentStatus.PAID,
            confirmed_at__gte=confirmed_since,
        )
        .annotate(paydoc=paydoc)
        .filter(Q(paydoc__isnull=True) | Q(paydoc=""))
    )
    return finding(queryset, limit=limit)


def open_refunds(requested_before: datetime, *, limit: int) -> Finding:
    """K3: refunds still open (``requested``, ``submitting``, ``unknown``, ``manual_required``) that were
    requested before ``requested_before``.

    Plan: the partial unique index ``refund_one_open_per_payment`` covers exactly the open refunds (one per
    payment at most); ``requested_at`` is filtered over them."""
    queryset = Refund.objects.filter(state__in=list(OPEN_REFUND_STATES), requested_at__lt=requested_before)
    return finding(queryset, limit=limit)


def failed_refunds_needing_action(*, limit: int) -> Finding:
    """K3: ``failed`` refunds that still need staff — the rule of admin contract §2.10
    ``attention.refunds_needing_action`` (not superseded by a later succeeded refund, payment not
    ``refunded``).

    Plan: ``refund_attention_idx`` (partial on failed/unknown/manual_required) range scan on ``state =
    'failed'``; the payment by its primary key and the later refunds through ``refund_payment_idx``."""
    queryset = Refund.objects.filter(state=enums.RefundState.FAILED).exclude(
        refund_reads.superseded_failure()
    )
    return finding(queryset, limit=limit)


def late_captures_not_refunded(confirmed_before: datetime, *, limit: int) -> Finding:
    """K5: refused captures (``error_code = 'late_capture'``, customer contract §2.13.0) captured before
    ``confirmed_before`` whose money is not back: still ``paid`` and no ``succeeded`` refund.

    Plan: ``payment_late_capture_open_idx`` (payments.0003, partial on late_capture ∧ paid) range scan on
    ``confirmed_at < confirmed_before``; the refunds through ``refund_payment_idx``."""
    succeeded = Refund.objects.filter(payment_id=OuterRef("pk"), state=enums.RefundState.SUCCEEDED)
    queryset = Payment.objects.filter(
        error_code=LATE_CAPTURE, status=enums.PaymentStatus.PAID, confirmed_at__lt=confirmed_before
    ).exclude(Exists(succeeded))
    return finding(queryset, limit=limit)


__all__ = [
    "click_paid_without_paydoc",
    "failed_refunds_needing_action",
    "late_captures_not_refunded",
    "open_refunds",
    "stuck_pending_payments",
]
