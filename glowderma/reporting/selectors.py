"""Read-side helpers of `reporting` for the shell (contract §2.10 ``nav-counts/``, §2.13 ``audit-log/``).

The dashboard selectors (§6.4) belong to another slice.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from django.db.models import QuerySet

from glowderma.appointments import selectors as appointment_selectors
from glowderma.commerce.models import Order
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry


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
    """The ``after`` image, falling back to ``before`` (contract §7 item 18); ``null`` when there is neither."""
    return entry.after if entry.after is not None else entry.before


def nav_counts() -> dict[str, int]:
    """``GET nav-counts/``: all-time ``processing`` orders and the pending-summary count."""
    return {
        "orders_processing": Order.objects.filter(status=enums.OrderStatus.PROCESSING).count(),
        "appointments_pending": appointment_selectors.pending_summary()["count"],
    }
