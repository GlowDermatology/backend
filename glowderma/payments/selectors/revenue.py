"""Dashboard revenue (contract §2.10, O8): per bucket, and per day and channel."""

from __future__ import annotations

from datetime import date, datetime

from django.db.models import (
    BigIntegerField,
    F,
    OuterRef,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncDay, TruncHour

from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.payments.models import (
    LATE_CAPTURE,
    SETTLED_PAYMENT_STATUSES,
    Payment,
    Refund,
)


def revenue_buckets(start: datetime, end: datetime, unit: str) -> dict[datetime, int]:
    """``{bucket start (Tashkent, aware): revenue}`` for payments confirmed in ``[start, end)``.

    Revenue = Σ ``amount`` of ``paid``/``refunded`` payments by confirmation time **minus** their succeeded
    refunds, attributed to the payment's own bucket — never negative (refunds ≤ amount). Late captures are
    excluded (customer contract §2.13.0: never order revenue). ``unit`` is ``hour`` or ``day``.
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
        .exclude(error_code=LATE_CAPTURE)
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


def revenue_by_day_and_channel(start: datetime, end: datetime) -> dict[tuple[date, str], int]:
    """``{(Tashkent day, order channel): revenue}`` for payments confirmed in ``[start, end)`` — the rule of
    :func:`revenue_buckets` (paid/refunded net of succeeded refunds, late captures excluded) split by the
    order's ``channel`` (the ops console's sales chart). One query over ``payment_confirmed_idx``."""
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
        .exclude(error_code=LATE_CAPTURE)
        .annotate(
            day=TruncDay("confirmed_at", tzinfo=TASHKENT),
            net=F("amount") - Coalesce(Subquery(refunded, output_field=BigIntegerField()), Value(0)),
        )
        .order_by()
        .values("day", "order__channel")
        .annotate(revenue=Sum("net"))
        .values_list("day", "order__channel", "revenue")
    )
    return {
        (day.astimezone(TASHKENT).date(), str(channel)): int(revenue or 0) for day, channel, revenue in rows
    }


__all__ = [
    "revenue_buckets",
    "revenue_by_day_and_channel",
]
