"""Read-side helpers of `reporting` for the shell (contract §2.10 ``dashboard/`` and ``nav-counts/``, §2.13
``audit-log/``). ``reporting`` owns no tables: it composes the lower modules' selectors (design 03 §6.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

from django.db.models import Count, Q, QuerySet

from glowderma.accounts.models import Customer
from glowderma.appointments import selectors as appointment_selectors
from glowderma.catalog import selectors as catalog_selectors
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery import selectors as delivery_selectors
from glowderma.integrations import selectors as integration_selectors
from glowderma.payments import selectors as payment_selectors


class AuditLogFilters(TypedDict, total=False):
    actor: int
    action: str
    object_type: str
    object_id: str
    date_from: date
    date_to: date
    ordering: str


def audit_log(filters: AuditLogFilters) -> QuerySet[AuditEntry]:
    """``GET audit-log/``: filters ``actor`` (staff id), ``action``, ``object_type``, ``object_id``,
    ``date_from`` / ``date_to`` (Tashkent days, inclusive); ordering ``-at`` (default) or ``at``."""
    qs = AuditEntry.objects.all()
    if "actor" in filters:
        qs = qs.filter(actor_type="staff", actor_id=filters["actor"])
    if "action" in filters:
        qs = qs.filter(action=filters["action"])
    if "object_type" in filters:
        qs = qs.filter(entity_type=filters["object_type"])
    if "object_id" in filters:
        qs = qs.filter(entity_id=filters["object_id"])
    if "date_from" in filters:
        qs = qs.filter(created_at__gte=timeutils.day_bounds(filters["date_from"])[0])
    if "date_to" in filters:
        qs = qs.filter(created_at__lt=timeutils.day_bounds(filters["date_to"])[1])
    if filters.get("ordering") == "at":
        return qs.order_by("created_at", "id")
    return qs.order_by("-created_at", "-id")


def audit_actor(entry: AuditEntry) -> dict[str, Any] | None:
    """``Actor | null``: staff entries carry the id and the label stored at write time; system is ``null``."""
    if entry.actor_type != "staff" or entry.actor_id is None:
        return None
    return {"id": entry.actor_id, "display_name": entry.actor_label}


def audit_data(entry: AuditEntry) -> Any:
    """The ``after`` image, else the ``before`` image (contract §7 item 18); ``null`` when neither exists."""
    return entry.after if entry.after is not None else entry.before


def nav_counts() -> dict[str, int]:
    """``GET nav-counts/``: all-time ``processing`` orders and the pending-summary count."""
    return {
        "orders_processing": commerce_selectors.processing_count(),
        "appointments_pending": appointment_selectors.pending_summary()["count"],
    }


# --- dashboard (contract §2.10, design 03 §6.4) -------------------------------------------------------------

HOUR_BUCKET_MAX_DAYS = 2  # custom ranges of ≤ 2 days are bucketed by hour
CUSTOM_MAX_DAYS = 366
WEEK_DAYS = 7
MONTH_DAYS = 30
DAY = enums.DashboardBucket.DAY


@dataclass(frozen=True, slots=True)
class DashboardWindow:
    """The resolved ``range``: inclusive Tashkent days ``[first, last]``, the bucket unit and the previous
    period of the same length just before ``first``."""

    range: str
    first: date
    last: date
    bucket: str

    @property
    def start(self) -> datetime:
        return timeutils.day_bounds(self.first)[0]

    @property
    def end(self) -> datetime:
        return timeutils.day_bounds(self.last)[1]

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    @property
    def previous(self) -> tuple[datetime, datetime]:
        first = self.first - timedelta(days=self.days)
        return timeutils.day_bounds(first)[0], self.start


def dashboard_window(
    range_: str, today: date, first: date | None = None, last: date | None = None
) -> DashboardWindow:
    """``today`` → 24 hour buckets of today; ``week`` → 7 days ending today; ``month`` → 30 days ending today
    (rolling); ``custom`` → ``[first, last]`` (validated by the caller), hour buckets iff ≤ 2 days."""
    if range_ == enums.DashboardRange.TODAY:
        return DashboardWindow(range_, today, today, enums.DashboardBucket.HOUR)
    if range_ == enums.DashboardRange.WEEK:
        return DashboardWindow(
            range_, today - timedelta(days=WEEK_DAYS - 1), today, enums.DashboardBucket.DAY
        )
    if range_ == enums.DashboardRange.MONTH:
        return DashboardWindow(
            range_, today - timedelta(days=MONTH_DAYS - 1), today, enums.DashboardBucket.DAY
        )
    if first is None or last is None:
        raise ValueError("A custom range needs from and to")
    days = (last - first).days + 1
    bucket = enums.DashboardBucket.HOUR if days <= HOUR_BUCKET_MAX_DAYS else enums.DashboardBucket.DAY
    return DashboardWindow(range_, first, last, bucket)


def bucket_starts(window: DashboardWindow) -> list[datetime]:
    """Every bucket start in the window (Tashkent has no DST, so hours are regular)."""
    step = timedelta(hours=1) if window.bucket == enums.DashboardBucket.HOUR else timedelta(days=1)
    starts: list[datetime] = []
    cursor = window.start
    while cursor < window.end:
        starts.append(cursor.astimezone(TASHKENT))
        cursor += step
    return starts


def customer_stats(start: datetime, end: datetime) -> dict[str, int]:
    """``new_customers`` (registered in the window, including since-deleted) and the all-time totals."""
    row = Customer.objects.aggregate(
        new=Count("id", filter=Q(registered_at__gte=start, registered_at__lt=end)),
        total=Count("id"),
        deleted=Count("id", filter=Q(status=enums.CustomerStatus.DELETED)),
    )
    return {
        "new_customers": int(row["new"] or 0),
        "total_customers": int(row["total"] or 0),
        "deleted_customers": int(row["deleted"] or 0),
    }


def bts_polling_failing() -> bool:
    """``attention.bts_polling_failing`` (admin §2.10, customer contract §2.13.3): the last BTS polling run
    stopped on transport failures or tracked no parcel (``IntegrationState(bts).consecutive_failures > 0``)."""
    state = integration_selectors.provider_state("bts")
    return state is not None and state.consecutive_failures > 0


def dashboard(window: DashboardWindow, now: datetime | None = None) -> dict[str, Any]:
    """``GET dashboard/``: buckets (``revenue`` = payments confirmed in the bucket net of their succeeded
    refunds; ``orders`` = orders created in the bucket), the previous period's orders, stats, attention."""
    instant = now or timeutils.now()
    orders = commerce_selectors.orders_in_buckets(window.start, window.end, window.bucket)
    revenue = payment_selectors.revenue_buckets(window.start, window.end, window.bucket)
    buckets = [
        {"start": start, "revenue": revenue.get(start, 0), "orders": orders.get(start, 0)}
        for start in bucket_starts(window)
    ]
    products = catalog_selectors.product_counts()
    pending = appointment_selectors.pending_summary(instant)
    return {
        "range": window.range,
        "from": window.first,
        "to": window.last,
        "bucket": window.bucket,
        "buckets": buckets,
        "previous_period_orders": commerce_selectors.count_created(*window.previous),
        "stats": {
            "orders": sum(bucket["orders"] for bucket in buckets),
            "revenue": sum(bucket["revenue"] for bucket in buckets),
            **customer_stats(window.start, window.end),
            "out_of_stock": products["out_of_stock"],
        },
        "attention": {
            "bts_failed": delivery_selectors.bts_failed_count(),
            "pending_appointments": pending["count"],
            "next_appointment_at": pending["next_at"],
            "new_from_billz": products["new_from_billz"],
            "refunds_needing_action": payment_selectors.refunds_needing_action(),
            "bts_polling_failing": bts_polling_failing(),
        },
        "generated_at": instant,
    }
