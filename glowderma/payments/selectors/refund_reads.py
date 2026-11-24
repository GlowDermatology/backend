"""Refund reads: one refund, an order's or a payment's refunds, and the count needing staff action."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from django.db.models import (
    Exists,
    OuterRef,
    Q,
)

from glowderma.core import enums
from glowderma.payments.models import (
    ATTENTION_REFUND_STATES,
    Refund,
)
from glowderma.payments.selectors import payment_rows


def get_refund(refund_id: int) -> payment_rows.RefundRow | None:
    refund = Refund.objects.select_related("payment").filter(pk=refund_id).first()
    return None if refund is None else payment_rows.refund_row(refund)


def refunds_for_order(order_id: int) -> list[payment_rows.RefundRow]:
    """Every refund of the order's payments, newest first (``requested_at desc, id desc``)."""
    rows = (
        Refund.objects.select_related("payment")
        .filter(payment__order_id=order_id)
        .order_by("-requested_at", "-id")
    )
    return [payment_rows.refund_row(refund) for refund in rows]


def refunds_by_payment(payment_ids: Iterable[int]) -> dict[int, list[payment_rows.RefundRow]]:
    """``{payment_id: [Refund newest first]}`` for a page of payments (one query)."""
    result: dict[int, list[payment_rows.RefundRow]] = defaultdict(list)
    rows = (
        Refund.objects.select_related("payment")
        .filter(payment_id__in=list(payment_ids))
        .order_by("-requested_at", "-id")
    )
    for refund in rows:
        result[refund.payment_id].append(payment_rows.refund_row(refund))
    return result


def superseded_failure() -> Q:
    """A ``failed`` refund that needs no action any more (admin contract §2.10, §2.12): a **later** refund of
    the same payment succeeded (a staff retry returned the money), or the payment is ``refunded``."""
    later_succeeded = Refund.objects.filter(
        payment_id=OuterRef("payment_id"), state=enums.RefundState.SUCCEEDED, pk__gt=OuterRef("pk")
    )
    return Q(state=enums.RefundState.FAILED) & (
        Q(payment__status=enums.PaymentStatus.REFUNDED) | Q(Exists(later_succeeded))
    )


def refunds_needing_action() -> int:
    """Dashboard ``attention.refunds_needing_action``: refunds ``failed``/``unknown``/``manual_required``,
    without the ``failed`` ones a later succeeded refund of the payment superseded or whose payment is
    ``refunded`` (admin contract §2.10)."""
    return (
        Refund.objects.filter(state__in=list(ATTENTION_REFUND_STATES)).exclude(superseded_failure()).count()
    )


__all__ = [
    "get_refund",
    "refunds_by_payment",
    "refunds_for_order",
    "refunds_needing_action",
    "superseded_failure",
]
