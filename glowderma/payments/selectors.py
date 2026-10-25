"""Read side of `payments` (design 02 §2.1 display payment, payments list, dashboard revenue; §2.2 refunds).

Rows leave the module as frozen dataclasses with the contract's field names (``transaction_id`` = the
provider transaction id, ``""`` → ``None``). A selector may traverse ``Payment``'s own FK upward
(``order__customer`` for the list and its search, 03 §0.1) but never a reverse relation of an upper module.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypedDict

from django.db.models import (
    BigIntegerField,
    Case,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, TruncDay, TruncHour

from glowderma.core import enums, timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.payments.models import (
    ATTENTION_REFUND_STATES,
    SETTLED_PAYMENT_STATUSES,
    Payment,
    Refund,
)

SEARCH_MIN_PHONE_DIGITS = 3


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
    )


# --- display payment (02 §2.1): rank refunded/paid 2 > pending 1 > failed 0, newest id first ---------------


def _rank() -> Case:
    return Case(
        When(status__in=list(SETTLED_PAYMENT_STATUSES), then=Value(2)),
        When(status=enums.PaymentStatus.PENDING, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )


def _ranked(qs: QuerySet[Payment]) -> QuerySet[Payment]:
    return qs.annotate(rank=_rank()).order_by("-rank", "-id")


def display_payment(order_id: int) -> PaymentRow | None:
    """The payment an order shows (``None`` when it has none yet)."""
    payment = _ranked(Payment.objects.filter(order_id=order_id)).first()
    return None if payment is None else payment_row(payment)


def pick_display(payments: Iterable[Payment]) -> Payment | None:
    """The display payment among an order's prefetched payments (same rank as :func:`display_payment`)."""

    def rank(payment: Payment) -> tuple[int, int]:
        if payment.status in SETTLED_PAYMENT_STATUSES:
            value = 2
        elif payment.status == enums.PaymentStatus.PENDING:
            value = 1
        else:
            value = 0
        return value, payment.pk

    rows = list(payments)
    return max(rows, key=rank) if rows else None


def display_status_subquery(order_ref: str = "pk") -> Coalesce:
    """``COALESCE(display payment status, 'pending')`` correlated on ``order_ref`` (``payment`` filter)."""
    ranked = _ranked(Payment.objects.filter(order_id=OuterRef(order_ref)))
    return Coalesce(Subquery(ranked.values("status")[:1]), Value(enums.PaymentStatus.PENDING))


def active_payment(order_id: int, status: str) -> PaymentRow | None:
    """The order's newest payment in ``status`` (``pending``: to confirm or fail; ``paid``: to refund)."""
    payment = Payment.objects.filter(order_id=order_id, status=status).order_by("-id").first()
    return None if payment is None else payment_row(payment)


def get_payment(payment_id: int) -> PaymentRow | None:
    payment = Payment.objects.filter(pk=payment_id).first()
    return None if payment is None else payment_row(payment)


# --- refunds ------------------------------------------------------------------------------------------------


def get_refund(refund_id: int) -> RefundRow | None:
    refund = Refund.objects.select_related("payment").filter(pk=refund_id).first()
    return None if refund is None else refund_row(refund)


def refunds_for_order(order_id: int) -> list[RefundRow]:
    """Every refund of the order's payments, newest first (``requested_at desc, id desc``)."""
    rows = (
        Refund.objects.select_related("payment")
        .filter(payment__order_id=order_id)
        .order_by("-requested_at", "-id")
    )
    return [refund_row(refund) for refund in rows]


def refunds_by_payment(payment_ids: Iterable[int]) -> dict[int, list[RefundRow]]:
    """``{payment_id: [Refund newest first]}`` for a page of payments (one query)."""
    result: dict[int, list[RefundRow]] = defaultdict(list)
    rows = (
        Refund.objects.select_related("payment")
        .filter(payment_id__in=list(payment_ids))
        .order_by("-requested_at", "-id")
    )
    for refund in rows:
        result[refund.payment_id].append(refund_row(refund))
    return result


def refunds_needing_action() -> int:
    """Dashboard ``attention.refunds_needing_action``: refunds ``failed``/``unknown``/``manual_required``."""
    return Refund.objects.filter(state__in=list(ATTENTION_REFUND_STATES)).count()


# --- payments list (contract §2.12) -------------------------------------------------------------------------


class PaymentFilters(TypedDict, total=False):
    status: str
    provider: str
    refund_state: str
    period: str
    date_from: date
    date_to: date
    search: str
    ordering: str


def search_q(term: str, *, id_field: str, phone_field: str) -> Q:
    """``№``-less id containment, or (≥ 3 digits) phone containment — the orders/payments search rule."""
    number = term.removeprefix("№").strip()
    condition = Q(**{f"{id_field}__contains": number}) if number else Q(pk__in=[])
    digits = re.sub(r"\D", "", term)
    if len(digits) >= SEARCH_MIN_PHONE_DIGITS:
        condition |= Q(**{f"{phone_field}__contains": digits})
    return condition


def payments_list(filters: PaymentFilters, now: datetime | None = None) -> QuerySet[Payment]:
    """``GET payments/``: filters ``status``, ``provider``, ``refund_state`` (any refund in that state),
    ``period``/``date_from``/``date_to`` on ``created_at`` (Tashkent, as ``orders/``), ``search`` (order id,
    transaction id, ≥ 3-digit phone); ordering ``±created_at`` with ties by ``id``."""
    instant = now or timeutils.now()
    qs = Payment.objects.select_related("order__customer")
    if "status" in filters:
        qs = qs.filter(status=filters["status"])
    if "provider" in filters:
        qs = qs.filter(provider=filters["provider"])
    if "refund_state" in filters:
        qs = qs.filter(
            Exists(Refund.objects.filter(payment_id=OuterRef("pk"), state=filters["refund_state"]))
        )
    bounds = timeutils.period_bounds(filters.get("period", enums.OrderPeriod.ALL), instant)
    qs = qs.filter(**{f"created_at__{key}": value for key, value in bounds.items()})
    date_bounds = timeutils.date_range_bounds(filters.get("date_from"), filters.get("date_to"))
    qs = qs.filter(**{f"created_at__{key}": value for key, value in date_bounds.items()})
    term = filters.get("search", "").strip()
    if term:
        qs = qs.annotate(order_id_text=Cast("order_id", output_field=TextField()))
        condition = search_q(term, id_field="order_id_text", phone_field="order__customer__phone")
        condition |= Q(provider_txn_id__icontains=term)
        qs = qs.filter(condition)
    if filters.get("ordering") == "created_at":
        return qs.order_by("created_at", "id")
    return qs.order_by("-created_at", "-id")


# --- dashboard revenue (contract §2.10, O8) -----------------------------------------------------------------


def revenue_buckets(start: datetime, end: datetime, unit: str) -> dict[datetime, int]:
    """``{bucket start (Tashkent, aware): revenue}`` for payments confirmed in ``[start, end)``.

    Revenue = Σ ``amount`` of ``paid``/``refunded`` payments by confirmation time **minus** their succeeded
    refunds, attributed to the payment's own bucket — never negative (refunds ≤ amount). ``unit`` is
    ``hour`` or ``day``.
    """
    trunc = TruncHour if unit == enums.DashboardBucket.HOUR else TruncDay
    refunded = (
        Refund.objects.filter(payment_id=OuterRef("pk"), state=enums.RefundState.SUCCEEDED)
        .order_by()
        .values("payment_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    rows = (
        Payment.objects.filter(
            status__in=list(SETTLED_PAYMENT_STATUSES), confirmed_at__gte=start, confirmed_at__lt=end
        )
        .annotate(
            bucket=trunc("confirmed_at", tzinfo=TASHKENT),
            net=F("amount") - Coalesce(Subquery(refunded, output_field=BigIntegerField()), Value(0)),
        )
        .order_by()
        .values("bucket")
        .annotate(revenue=Sum("net"))
        .values_list("bucket", "revenue")
    )
    return {bucket.astimezone(TASHKENT): int(revenue or 0) for bucket, revenue in rows}


__all__ = [
    "PaymentFilters",
    "PaymentRow",
    "RefundRow",
    "active_payment",
    "display_payment",
    "display_status_subquery",
    "get_payment",
    "get_refund",
    "payment_row",
    "payments_list",
    "pick_display",
    "refund_row",
    "refunds_by_payment",
    "refunds_for_order",
    "refunds_needing_action",
    "revenue_buckets",
    "search_q",
]
