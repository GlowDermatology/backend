"""The admin payments list (contract §2.12): its filters and search."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TypedDict

from django.db.models import (
    Exists,
    OuterRef,
    Q,
    QuerySet,
    TextField,
)
from django.db.models.functions import Cast

from glowderma.core import enums, timeutils
from glowderma.payments.models import (
    Payment,
    Refund,
)
from glowderma.payments.selectors import refund_reads

SEARCH_MIN_PHONE_DIGITS = 3


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
        refunds = Refund.objects.filter(payment_id=OuterRef("pk"), state=filters["refund_state"])
        if filters["refund_state"] == enums.RefundState.FAILED:
            refunds = refunds.exclude(
                refund_reads.superseded_failure()
            )  # admin contract §2.12: same rule as §2.10
        qs = qs.filter(Exists(refunds))
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


__all__ = [
    "SEARCH_MIN_PHONE_DIGITS",
    "PaymentFilters",
    "payments_list",
    "search_q",
]
