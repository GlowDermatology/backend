"""Read-side helpers of `appointments` (design 03 §2.1 "Contract fields served")."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Max, Min, Q, QuerySet

from glowderma.appointments.models import OPEN_STATUSES, Appointment
from glowderma.core import enums, timeutils
from glowderma.professionals.models import Professional, ProfessionalService, Service
from glowderma.professionals.selectors import (
    DAY_END_LABEL,
    DaySchedule,
    booking_catalogue,
    day_schedules,
    schedules_for_days,
)


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
    """``GET appointments/?date=``: the schedules (01 §5.8) of the active specialists and of every inactive
    one that still has an appointment that day (a specialist deactivated with ``force`` keeps a column for
    the appointments it left behind), and the day's appointments."""
    appointments = list(day_appointments(day))
    with_rows = {appointment.professional_id for appointment in appointments}
    professionals = Professional.objects.filter(Q(is_active=True) | Q(pk__in=with_rows))
    return DayView(date=day, specialists=day_schedules(day, professionals), appointments=appointments)


def pending_summary(now: datetime | None = None) -> dict[str, Any]:
    """``{"count", "next_at"}`` of the ``requested`` appointments that still await an answer (contract §2.9):
    those starting today (Tashkent) or later — today's requests whose time has already passed included, so
    the header never says "all reviewed" while one of them is unanswered; requests of earlier days are not
    counted. ``next_at`` is the earliest of them (it may lie earlier today)."""
    instant = timeutils.now() if now is None else now
    today_start, _ = timeutils.day_bounds(timeutils.local_date(instant))
    summary = Appointment.objects.filter(
        status=enums.AppointmentStatus.REQUESTED, start__gte=today_start
    ).aggregate(count=Count("id"), next_at=Min("start"))
    return {"count": int(summary["count"] or 0), "next_at": summary["next_at"]}


def upcoming_counts_by_professional(professional_ids: Collection[int], instant: datetime) -> dict[int, int]:
    """``{professional_id: n}`` of the open (requested|confirmed) appointments starting at or after
    ``instant`` — the counter ``professionals.bookings`` asks before deactivating a specialist (one query)."""
    rows = (
        Appointment.objects.filter(
            professional_id__in=list(professional_ids), status__in=OPEN_STATUSES, start__gte=instant
        )
        .values("professional_id")
        .annotate(n=Count("id"))
        .order_by()
    )
    return {int(row["professional_id"]): int(row["n"]) for row in rows}


def schedule_conflicts(professional: Professional, now: datetime | None = None) -> list[Appointment]:
    """``GET specialists/{id}/conflicts/``: the specialist's open appointments starting now or later that its
    current schedule no longer covers — a day without hours (day off, no weekly row), a range outside that
    day's hours (an override wins), or an overlap with a blocked period or the recurring break. Schedule
    changes never move or cancel appointments; this is how staff find the ones to reschedule. Four queries
    whatever the number of days."""
    instant = timeutils.now() if now is None else now
    rows = list(
        appointment_queryset()
        .filter(professional=professional, status__in=OPEN_STATUSES, start__gte=instant)
        .order_by("start", "id")
    )
    if not rows:
        return []
    schedules = schedules_for_days(professional, {timeutils.local_date(row.start) for row in rows})
    conflicts: list[Appointment] = []
    for row in rows:
        day = timeutils.local_date(row.start)
        schedule = schedules[day]
        if schedule.working_hours is None:
            conflicts.append(row)
            continue
        hours_start, hours_end = (clock(day, label) for label in schedule.working_hours)
        blocks = [(clock(day, block.start), clock(day, block.end)) for block in schedule.blocks]
        if row.start < hours_start or row.end > hours_end or _overlaps(row.start, row.end, blocks):
            conflicts.append(row)
    return conflicts


def upcoming_count(customer_id: int, now: datetime | None = None) -> int:
    """Customer card ``upcoming_appointments``: open (requested|confirmed) rows with ``start ≥ now``."""
    instant = timeutils.now() if now is None else now
    return Appointment.objects.filter(
        customer_id=customer_id, status__in=OPEN_STATUSES, start__gte=instant
    ).count()


# --- M2 (plan §1 P0): the appointment part of the dormant-account rule (customer contract §2.2.3) ---


def last_activity_at(customer_id: int) -> datetime | None:
    """The customer's newest appointment activity, else ``None``.

    The newest of every appointment's ``created_at`` (booked in the app **or by staff**, any status) and the
    ``start`` of ``requested`` / ``confirmed`` / ``attended`` appointments (a future open appointment counts
    with its future start, so an account with one is never dormant). ``customers.selectors.last_activity_at``
    (P1) combines it with sign-ins, purchases and orders.
    """
    rows = Appointment.objects.filter(customer_id=customer_id)
    created = rows.aggregate(newest=Max("created_at"))["newest"]
    started = rows.filter(
        status__in=(
            enums.AppointmentStatus.REQUESTED,
            enums.AppointmentStatus.CONFIRMED,
            enums.AppointmentStatus.ATTENDED,
        )
    ).aggregate(newest=Max("start"))["newest"]
    candidates = [value for value in (created, started) if value is not None]
    return max(candidates) if candidates else None


# --- M2 customer API (customer contract §2.11, plan §4 P3) ---


def clock(day: date, label: str) -> datetime:
    """A ``"HH:MM"`` Tashkent wall-clock label of ``day`` as an aware instant (``23:59`` = the day's end:
    a block running to or past midnight is clipped to that label by ``day_schedules``)."""
    if label == DAY_END_LABEL:
        return timeutils.day_bounds(day)[1]
    hour, minute = (int(part) for part in label.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=timeutils.TASHKENT)


def customer_today(now: datetime | None = None) -> date:
    return timeutils.local_date(timeutils.now() if now is None else now)


def booking_horizon_end(now: datetime | None = None) -> date:
    """The last bookable Tashkent day: today + ``BOOKING_HORIZON_DAYS``."""
    return customer_today(now) + timedelta(days=int(settings.BOOKING_HORIZON_DAYS))


def lead_time() -> timedelta:
    return timedelta(minutes=int(settings.BOOKING_MIN_LEAD_MINUTES))


@dataclass(frozen=True, slots=True)
class DayFrame:
    """The bookable frame of one specialist on one day: hours (``None`` = not working) and the busy ranges
    (blocked periods incl. the recurring break, and the open appointments of the specialist)."""

    hours: tuple[datetime, datetime] | None
    hours_labels: tuple[str, str] | None
    busy: list[tuple[datetime, datetime]]
    blocks: list[tuple[datetime, datetime]]


def day_frame(professional_id: int, day: date, *, exclude_pk: int | None = None) -> DayFrame:
    [schedule] = day_schedules(day, Professional.objects.filter(pk=professional_id))
    if schedule.working_hours is None:
        return DayFrame(hours=None, hours_labels=None, busy=[], blocks=[])
    hours = (clock(day, schedule.working_hours[0]), clock(day, schedule.working_hours[1]))
    blocks = [(clock(day, block.start), clock(day, block.end)) for block in schedule.blocks]
    day_start, day_end = timeutils.day_bounds(day)
    appointments = Appointment.objects.filter(
        professional_id=professional_id, status__in=OPEN_STATUSES, start__lt=day_end, end__gt=day_start
    )
    if exclude_pk is not None:
        appointments = appointments.exclude(pk=exclude_pk)
    busy = [*blocks, *appointments.values_list("start", "end")]
    return DayFrame(hours=hours, hours_labels=schedule.working_hours, busy=busy, blocks=blocks)


def _overlaps(start: datetime, end: datetime, ranges: list[tuple[datetime, datetime]]) -> bool:
    return any(other_start < end and other_end > start for other_start, other_end in ranges)


@dataclass(frozen=True, slots=True)
class FreeSlots:
    """``GET specialists/{id}/slots/`` (C§2.11): the link's duration and price, the grid step, the day's
    hours and the free start times."""

    day: date
    professional_id: int
    service_id: int
    duration_min: int
    price: int
    slot_step_min: int
    working_hours: tuple[str, str] | None
    slots: list[datetime]


def free_slots(
    professional: Professional, link: ProfessionalService, day: date, now: datetime | None = None
) -> FreeSlots:
    """The C§2.11 algorithm: candidates ``t = hours.start + k·step`` with ``t + duration ≤ hours.end``; drop
    ``t < now + BOOKING_MIN_LEAD_MINUTES`` and any overlap of ``[t, t + duration)`` with blocked periods, the
    recurring break and the ``requested``/``confirmed`` appointments of the specialist (pending requests hold
    the slot)."""
    instant = timeutils.now() if now is None else now
    frame = day_frame(professional.pk, day)
    step = timedelta(minutes=professional.slot_step_min)
    duration = timedelta(minutes=link.duration_min)
    slots: list[datetime] = []
    if frame.hours is not None:
        earliest = instant + lead_time()
        candidate, hours_end = frame.hours
        while candidate + duration <= hours_end:
            if candidate >= earliest and not _overlaps(candidate, candidate + duration, frame.busy):
                slots.append(candidate)
            candidate += step
    return FreeSlots(
        day=day,
        professional_id=professional.pk,
        service_id=link.service_id,
        duration_min=link.duration_min,
        price=link.price,
        slot_step_min=professional.slot_step_min,
        working_hours=frame.hours_labels,
        slots=slots,
    )


def on_grid(professional: Professional, frame: DayFrame, start: datetime) -> bool:
    """``(start − hours.start) mod slot_step_min = 0`` with zero seconds (a day off has no grid)."""
    if frame.hours is None or start.second or start.microsecond:
        return False
    offset = start - frame.hours[0]
    return offset % timedelta(minutes=professional.slot_step_min) == timedelta(0)


def overlaps_busy(frame: DayFrame, start: datetime, end: datetime) -> bool:
    return _overlaps(start, end, frame.busy)


def customer_open_overlap(customer_id: int, start: datetime, end: datetime) -> bool:
    return Appointment.objects.filter(
        customer_id=customer_id, status__in=OPEN_STATUSES, start__lt=end, end__gt=start
    ).exists()


def customer_open_future_count(customer_id: int, now: datetime | None = None) -> int:
    """Open (``requested``/``confirmed``) appointments of the customer whose start is still ahead (the
    ``BOOKING_MAX_OPEN_PER_CUSTOMER`` count)."""
    return upcoming_count(customer_id, now)


def _horizon_filter(qs: QuerySet[Appointment], horizon: datetime | None) -> QuerySet[Appointment]:
    return qs if horizon is None else qs.filter(created_at__gte=horizon)


def customer_appointment_queryset() -> QuerySet[Appointment]:
    return Appointment.objects.select_related("service", "professional", "professional__location")


def customer_appointments(
    customer_id: int, scope: str, *, horizon: datetime | None, now: datetime | None = None
) -> QuerySet[Appointment]:
    """``GET me/appointments/?scope=``: ``upcoming`` = open with ``start ≥ now``, ascending by start;
    ``past`` = the rest, newest start first; both limited to the history horizon (rows created before
    ``Customer.history_from`` belong to a previous holder of the number, C§2.2.3)."""
    instant = timeutils.now() if now is None else now
    upcoming = Q(status__in=OPEN_STATUSES, start__gte=instant)
    qs = _horizon_filter(customer_appointment_queryset().filter(customer_id=customer_id), horizon)
    if scope == enums.AppointmentScope.UPCOMING:
        return qs.filter(upcoming).order_by("start", "id")
    return qs.exclude(upcoming).order_by("-start", "-id")


def customer_appointment(customer_id: int, pk: int, *, horizon: datetime | None) -> Appointment | None:
    """One own appointment inside the horizon, else ``None`` (the view answers 404)."""
    qs = customer_appointment_queryset().filter(customer_id=customer_id, pk=pk)
    return _horizon_filter(qs, horizon).first()


def can_cancel(appointment: Appointment, now: datetime | None = None) -> bool:
    """Open ∧ ``start − now ≥ BOOKING_CANCEL_MIN_LEAD_MINUTES`` (C§2.11)."""
    instant = timeutils.now() if now is None else now
    lead = timedelta(minutes=int(settings.BOOKING_CANCEL_MIN_LEAD_MINUTES))
    return appointment.status in OPEN_STATUSES and appointment.start - instant >= lead


# --- the public booking catalogue (C§2.11: specialists and services) ---


def bookable_links(professional: Professional) -> list[ProfessionalService]:
    """The active links to active services prefetched by ``booking_catalogue``."""
    return list(getattr(professional, "bookable_links", []))


def specialists() -> list[Professional]:
    """Active professionals by position, name, with their bookable links and location."""
    return list(booking_catalogue().select_related("location"))


def specialist(pk: int) -> Professional | None:
    return booking_catalogue().select_related("location").filter(pk=pk).first()


def offered_link(professional_id: int, service_id: int) -> ProfessionalService | None:
    """The active link of an active service of an active professional, else ``None``."""
    return (
        ProfessionalService.objects.select_related("service", "professional")
        .filter(
            professional_id=professional_id,
            professional__is_active=True,
            service_id=service_id,
            service__is_active=True,
            is_active=True,
        )
        .first()
    )


@dataclass(frozen=True, slots=True)
class ServiceOffer:
    service: Service
    price_from: int
    price_to: int
    specialist_ids: list[int]


def service_offers() -> list[ServiceOffer]:
    """Active services offered by at least one active specialist through an active link, by position and
    name, with the price range and the specialist ids."""
    links = (
        ProfessionalService.objects.filter(
            is_active=True, service__is_active=True, professional__is_active=True
        )
        .select_related("service")
        .order_by(
            "service__position", "service__name_uz", "service_id", "professional__position", "professional_id"
        )
    )
    offers: dict[int, ServiceOffer] = {}
    for link in links:
        offer = offers.get(link.service_id)
        if offer is None:
            offers[link.service_id] = ServiceOffer(
                link.service, link.price, link.price, [link.professional_id]
            )
            continue
        offers[link.service_id] = ServiceOffer(
            offer.service,
            min(offer.price_from, link.price),
            max(offer.price_to, link.price),
            [*offer.specialist_ids, link.professional_id],
        )
    return list(offers.values())
