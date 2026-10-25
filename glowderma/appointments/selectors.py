"""Read-side helpers of `appointments` (design 03 §2.1 "Contract fields served")."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.db.models import Count, Min, QuerySet

from glowderma.appointments.models import OPEN_STATUSES, Appointment
from glowderma.core import enums, timeutils
from glowderma.professionals.selectors import DaySchedule, day_schedules


def appointment_queryset() -> QuerySet[Appointment]:
    return Appointment.objects.select_related("customer", "service", "professional")


def get_appointment(pk: int) -> Appointment | None:
    return appointment_queryset().filter(pk=pk).first()


def day_appointments(day: date) -> QuerySet[Appointment]:
    """Rows whose Tashkent day of ``start`` is ``day``, all statuses, ``ORDER BY start, id``."""
    start, end = timeutils.day_bounds(day)
    return appointment_queryset().filter(start__gte=start, start__lt=end).order_by("start", "id")


@dataclass(frozen=True, slots=True)
class DayView:
    date: date
    specialists: list[DaySchedule]
    appointments: list[Appointment]


def day_view(day: date) -> DayView:
    """``GET appointments/?date=``: the active specialists' schedules (01 §5.8) and the day's appointments."""
    return DayView(date=day, specialists=day_schedules(day), appointments=list(day_appointments(day)))


def pending_summary(now: datetime | None = None) -> dict[str, Any]:
    """``{"count", "next_at"}`` of ``requested`` appointments with ``start ≥ now`` (contract §2.9)."""
    instant = timeutils.now() if now is None else now
    summary = Appointment.objects.filter(
        status=enums.AppointmentStatus.REQUESTED, start__gte=instant
    ).aggregate(count=Count("id"), next_at=Min("start"))
    return {"count": int(summary["count"] or 0), "next_at": summary["next_at"]}


def upcoming_count(customer_id: int, now: datetime | None = None) -> int:
    """Customer card ``upcoming_appointments``: open (requested|confirmed) rows with ``start ≥ now``."""
    instant = timeutils.now() if now is None else now
    return Appointment.objects.filter(
        customer_id=customer_id, status__in=OPEN_STATUSES, start__gte=instant
    ).count()
