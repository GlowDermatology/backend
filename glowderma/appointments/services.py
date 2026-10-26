"""Writes of `appointments` (design 03 §2.1 state machine; T10 admin creation / rescheduling / cancellation).

Every transition runs under ``select_for_update()`` of the row, writes one ``AppointmentEvent`` through
:func:`record_event`, enqueues the ``push.appointment`` outbox message where the design says so and audits
``appointment.<action>``. Slot rules live in :func:`check_slot`; the ``btree_gist`` exclusion constraint is
the last line of defence against a concurrent booking (``IntegrityError`` → 409 ``conflict``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts.models import Customer, Device
from glowderma.appointments import selectors, topics
from glowderma.appointments.models import OPEN_STATUSES, Appointment, AppointmentEvent
from glowderma.core import audit, enums, outbox, timeutils
from glowderma.core.api.exceptions import (
    AuthFailed,
    BookingLimit,
    Conflict,
    InvalidTransition,
    SlotTaken,
    TermsUpdateRequired,
    field_error,
    not_cancellable,
)
from glowderma.professionals.models import Professional, ProfessionalService, Service
from glowderma.professionals.selectors import DAY_END_LABEL, day_schedules
from glowderma.site_config import selectors as site_config_selectors

SLOT_STEP_MIN = 5  # admin bookings sit on a 5-minute grid (open point A-4: not the professional's slot_step)
MESSAGES = {
    "invalid_transition": "Bu amal ushbu holatda mumkin emas.",
    "slot_taken": "Bu vaqt band.",
    "outside_working_hours": "Mutaxassis bu vaqtda ishlamaydi.",
    "in_past": "Kelajakdagi vaqtni tanlang.",
    "grid": "Vaqt 5 daqiqalik qadamda bo‘lsin.",
    "service_not_offered": "Mutaxassis bu xizmatni ko‘rsatmaydi.",
    "specialist_not_found": "Mutaxassis topilmadi.",
}
# ``POST appointments/{id}/{action}/``: allowed source statuses and the resulting status (contract §2.9).
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    enums.AppointmentAction.CONFIRM: (
        (enums.AppointmentStatus.REQUESTED,),
        enums.AppointmentStatus.CONFIRMED,
    ),
    enums.AppointmentAction.REJECT: ((enums.AppointmentStatus.REQUESTED,), enums.AppointmentStatus.REJECTED),
    enums.AppointmentAction.ATTEND: ((enums.AppointmentStatus.CONFIRMED,), enums.AppointmentStatus.ATTENDED),
}
PUSHED_ACTIONS: dict[str, str] = {
    enums.AppointmentAction.CONFIRM: "confirmed",
    enums.AppointmentAction.REJECT: "rejected",
}

# Test seam for the booking race: called after the slot checks and before the row is written.
_before_write: Callable[[], None] = lambda: None  # noqa: E731
DEADLOCK_DETECTED = "40P01"
EXCLUSION_VIOLATION = "23P01"
NO_OVERLAP_CONSTRAINT = "appointment_no_overlap"


def _lost_the_slot(exc: Exception) -> bool:
    """Whether a write error means a concurrent booking won the slot.

    Two transactions inserting overlapping rows each find the other's uncommitted tuple while checking the
    ``appointment_no_overlap`` exclusion constraint; PostgreSQL then either makes one wait and fails it with
    the exclusion violation (``IntegrityError`` 23P01 on that constraint) or detects the mutual wait and
    aborts one with a deadlock (``OperationalError`` 40P01). Both are the same lost race; any other
    integrity error (a check constraint, a foreign key to a row deleted meanwhile) is a real error.
    """
    cause = exc.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if isinstance(exc, IntegrityError):
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
        return sqlstate == EXCLUSION_VIOLATION and constraint == NO_OVERLAP_CONSTRAINT
    return sqlstate == DEADLOCK_DETECTED


def _start_error(code: str, message: str) -> ValidationError:
    return ValidationError({"start": [field_error(code, message)]})


def _clock(day: date, label: str) -> datetime:
    if label == DAY_END_LABEL:
        return timeutils.day_bounds(day)[1]
    hour, minute = (int(part) for part in label.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=timeutils.TASHKENT)


def check_slot(
    professional: Professional, start: datetime, end: datetime, *, exclude_pk: int | None = None
) -> None:
    """Admin slot rules: 5-minute grid, not in the past, inside the day's hours (override wins), not
    overlapping a blocked period (incl. the recurring break) nor another open appointment of the
    specialist."""
    local = start.astimezone(timeutils.TASHKENT)
    if local.second or local.microsecond or local.minute % SLOT_STEP_MIN:
        raise _start_error("invalid", MESSAGES["grid"])
    if start < timeutils.now():
        raise _start_error("in_past", MESSAGES["in_past"])
    day = local.date()
    [schedule] = day_schedules(day, Professional.objects.filter(pk=professional.pk))
    if schedule.working_hours is None:
        raise _start_error("outside_working_hours", MESSAGES["outside_working_hours"])
    hours_start, hours_end = (_clock(day, label) for label in schedule.working_hours)
    if start < hours_start or end > hours_end:
        raise _start_error("outside_working_hours", MESSAGES["outside_working_hours"])
    for block in schedule.blocks:
        if _clock(day, block.start) < end and _clock(day, block.end) > start:
            raise _start_error("slot_unavailable", MESSAGES["slot_taken"])
    overlapping = Appointment.objects.filter(
        professional=professional, status__in=OPEN_STATUSES, start__lt=end, end__gt=start
    )
    if exclude_pk is not None:
        overlapping = overlapping.exclude(pk=exclude_pk)
    if overlapping.exists():
        raise _start_error("slot_unavailable", MESSAGES["slot_taken"])


def _hold_active_specialist(professional_id: int) -> bool:
    """Lock the specialist's row ``FOR KEY SHARE`` for the rest of the transaction and re-read ``is_active``.

    The deactivation of the admin specialists API counts the open appointments under ``SELECT … FOR UPDATE``
    of the same row, which conflicts with this lock (bookings among themselves do not: key-share locks are
    compatible). Either the deactivation waits for this booking and then counts it (409 unless ``force``),
    or this booking waits for the deactivation and then reads ``is_active = false`` (refused). The foreign
    key of the new appointment cannot serve: Django creates it ``DEFERRABLE INITIALLY DEFERRED``, so its
    check runs only at commit, after the guard.
    """
    table = connection.ops.quote_name(Professional._meta.db_table)
    with connection.cursor() as cursor:
        # The table name comes from the model's metadata, the id is a bound parameter.
        cursor.execute(f"SELECT is_active FROM {table} WHERE id = %s FOR KEY SHARE", [professional_id])  # noqa: S608
        row = cursor.fetchone()
    return bool(row and row[0])


def _specialist_gone() -> ValidationError:
    return ValidationError(
        {"specialist_id": [field_error("does_not_exist", MESSAGES["specialist_not_found"])]}
    )


def active_link(professional: Professional, service: Service) -> ProfessionalService:
    link = ProfessionalService.objects.filter(
        professional=professional, service=service, is_active=True
    ).first()
    if link is None:
        raise ValidationError(
            {"service_id": [field_error("does_not_exist", MESSAGES["service_not_offered"])]}
        )
    return link


def _pushable(customer_id: int) -> bool:
    return Device.objects.filter(customer_id=customer_id, is_active=True, push_enabled=True).exists()


def record_event(
    appointment: Appointment, type_: str, *, source: str, actor: Any = None, data: dict[str, Any]
) -> AppointmentEvent:
    """The only ``AppointmentEvent`` writer (design 03 §2.2)."""
    return AppointmentEvent.objects.create(
        appointment=appointment, type=type_, source=source, actor=actor, data=data
    )


def _enqueue_push(appointment: Appointment, action: str, event: AppointmentEvent) -> None:
    reschedule_no = appointment.reschedule_count if action == "rescheduled" else None
    outbox.enqueue(
        topics.PUSH_APPOINTMENT,
        {
            "appointment_id": appointment.pk,
            "action": action,
            "event_id": event.pk,
            "customer_id": appointment.customer_id,
            "reschedule_no": reschedule_no,
        },
        dedupe_key=topics.push_appointment_key(appointment.pk, action, reschedule_no),
    )


def _audit(appointment: Appointment, action: str, *, actor: Any, request: Any, **extra: Any) -> None:
    audit.record(
        action=f"appointment.{action}",
        entity_type="appointment",
        entity_id=str(appointment.pk),
        actor=actor,
        request=request,
        after={
            "status": appointment.status,
            "professional_id": appointment.professional_id,
            "customer_id": appointment.customer_id,
            "service_id": appointment.service_id,
            "start": timeutils.isoformat(appointment.start),
            "end": timeutils.isoformat(appointment.end),
            **extra,
        },
    )


def _locked(appointment_id: int) -> Appointment:
    return Appointment.objects.select_for_update().get(pk=appointment_id)


def _range_data(appointment: Appointment) -> dict[str, Any]:
    return {
        "start": timeutils.isoformat(appointment.start),
        "end": timeutils.isoformat(appointment.end),
        "service_id": appointment.service_id,
        "professional_id": appointment.professional_id,
        "price": appointment.price,
        "duration_min": appointment.duration_min,
    }


# --- admin transitions (contract §2.9) --------------------------------------------------------------------


@transaction.atomic
def transition(appointment_id: int, action: str, *, actor: Any, request: Any = None) -> Appointment:
    """``confirm`` / ``reject`` / ``attend``; any other source status → 409 ``invalid_transition``."""
    allowed_from, target = TRANSITIONS[action]
    appointment = _locked(appointment_id)
    if appointment.status not in allowed_from:
        raise InvalidTransition(MESSAGES["invalid_transition"])
    now = timeutils.now()
    appointment.status = target
    setattr(appointment, f"{target}_at", now)
    appointment.save(update_fields=["status", f"{target}_at"])
    pushed = PUSHED_ACTIONS.get(action)
    data: dict[str, Any] = {"push_sent": _pushable(appointment.customer_id)} if pushed else {}
    event = record_event(appointment, target, source=AppointmentEvent.Source.ADMIN, actor=actor, data=data)
    if pushed:
        _enqueue_push(appointment, pushed, event)
    _audit(appointment, target, actor=actor, request=request)
    return appointment


def confirm(appointment_id: int, *, actor: Any, request: Any = None) -> Appointment:
    return transition(appointment_id, enums.AppointmentAction.CONFIRM, actor=actor, request=request)


def reject(appointment_id: int, *, actor: Any, request: Any = None) -> Appointment:
    return transition(appointment_id, enums.AppointmentAction.REJECT, actor=actor, request=request)


def attend(appointment_id: int, *, actor: Any, request: Any = None) -> Appointment:
    return transition(appointment_id, enums.AppointmentAction.ATTEND, actor=actor, request=request)


# --- admin create / reschedule / cancel (T10, contract §7 item 18) ------------------------------------------


def create_by_admin(
    *,
    professional: Professional,
    service: Service,
    customer: Customer,
    start: datetime,
    note: str = "",
    actor: Any,
    request: Any = None,
) -> Appointment:
    """A confirmed booking made by staff: ``requested_via = admin``, events ``requested`` + ``confirmed``."""
    link = active_link(professional, service)
    end = start + timedelta(minutes=link.duration_min)
    check_slot(professional, start, end)
    _before_write()
    now = timeutils.now()
    try:
        with transaction.atomic():
            if not _hold_active_specialist(professional.pk):  # deactivated since the serializer read it
                raise _specialist_gone()
            appointment = Appointment.objects.create(
                professional=professional,
                service=service,
                customer=customer,
                start=start,
                end=end,
                duration_min=link.duration_min,
                price=link.price,
                status=enums.AppointmentStatus.CONFIRMED,
                requested_via=enums.RequestedVia.ADMIN,
                requested_at=now,
                created_by=actor,
                staff_note=note,
                confirmed_at=now,
            )
            record_event(
                appointment,
                AppointmentEvent.Type.REQUESTED,
                source=AppointmentEvent.Source.ADMIN,
                actor=actor,
                data={"via": enums.RequestedVia.ADMIN, **_range_data(appointment)},
            )
            record_event(
                appointment,
                AppointmentEvent.Type.CONFIRMED,
                source=AppointmentEvent.Source.ADMIN,
                actor=actor,
                data={"push_sent": False},
            )
            _audit(
                appointment, "created", actor=actor, request=request, requested_via=enums.RequestedVia.ADMIN
            )
    except (IntegrityError, OperationalError) as exc:  # appointment_no_overlap: a concurrent booking won
        if not _lost_the_slot(exc):
            raise
        raise Conflict(MESSAGES["slot_taken"]) from exc
    return appointment


@transaction.atomic
def reschedule(
    appointment_id: int,
    *,
    start: datetime,
    professional: Professional | None = None,
    actor: Any,
    request: Any = None,
) -> Appointment:
    """Move an open appointment (status unchanged). A new specialist re-snapshots duration and price from
    that specialist's service link; the same specialist keeps the booked duration and price."""
    appointment = _locked(appointment_id)
    if appointment.status not in OPEN_STATUSES:
        raise InvalidTransition(MESSAGES["invalid_transition"])
    target = professional or appointment.professional
    duration, price = appointment.duration_min, appointment.price
    if target.pk != appointment.professional_id:
        if not _hold_active_specialist(target.pk):  # deactivated since the serializer read it
            raise _specialist_gone()
        link = active_link(target, appointment.service)
        duration, price = link.duration_min, link.price
    end = start + timedelta(minutes=duration)
    check_slot(target, start, end, exclude_pk=appointment.pk)
    _before_write()
    before = {
        "from_start": timeutils.isoformat(appointment.start),
        "from_end": timeutils.isoformat(appointment.end),
    }
    from_professional = appointment.professional_id
    appointment.professional = target
    appointment.start, appointment.end = start, end
    appointment.duration_min, appointment.price = duration, price
    appointment.reschedule_count += 1
    try:
        with transaction.atomic():
            appointment.save(
                update_fields=["professional", "start", "end", "duration_min", "price", "reschedule_count"]
            )
    except (IntegrityError, OperationalError) as exc:
        if not _lost_the_slot(exc):
            raise
        raise Conflict(MESSAGES["slot_taken"]) from exc
    event = record_event(
        appointment,
        AppointmentEvent.Type.RESCHEDULED,
        source=AppointmentEvent.Source.ADMIN,
        actor=actor,
        data={
            **before,
            "to_start": timeutils.isoformat(start),
            "to_end": timeutils.isoformat(end),
            "from_professional_id": from_professional,
            "to_professional_id": target.pk,
            "by": "admin",
            "push_sent": _pushable(appointment.customer_id),
        },
    )
    _enqueue_push(appointment, "rescheduled", event)
    _audit(
        appointment,
        "rescheduled",
        actor=actor,
        request=request,
        **before,
        reschedule_no=appointment.reschedule_count,
    )
    return appointment


@transaction.atomic
def cancel(appointment_id: int, *, reason: str = "", actor: Any, request: Any = None) -> Appointment:
    """Admin cancellation of an open appointment; the slot is freed and the customer is pushed."""
    appointment = _locked(appointment_id)
    if appointment.status not in OPEN_STATUSES:
        raise InvalidTransition(MESSAGES["invalid_transition"])
    appointment.status = enums.AppointmentStatus.CANCELLED
    appointment.cancelled_at = timeutils.now()
    appointment.cancelled_by = Appointment.CancelledBy.ADMIN
    fields = ["status", "cancelled_at", "cancelled_by"]
    if reason:
        appointment.staff_note = reason
        fields.append("staff_note")
    appointment.save(update_fields=fields)
    event = record_event(
        appointment,
        AppointmentEvent.Type.CANCELLED,
        source=AppointmentEvent.Source.ADMIN,
        actor=actor,
        data={"by": "admin", "reason": reason or None, "push_sent": _pushable(appointment.customer_id)},
    )
    _enqueue_push(appointment, "cancelled", event)
    _audit(appointment, "cancelled", actor=actor, request=request, reason=reason or None)
    return appointment


# --- M2 customer booking and cancel (customer contract §2.11, plan §4 P3) -----------------------------------

ACCOUNT_DELETED_REASON = "account_deleted"  # passed by ``DELETE me/`` (P1): open items are cancelled at once


def _check_consent(customer: Customer) -> None:
    """409 ``terms_update_required`` while the customer's consent is older than the published terms."""
    current = site_config_selectors.outdated_consent(customer.terms_version)
    if current is not None:
        raise TermsUpdateRequired(details={"current_version": current})


def _field(name: str, code: str, key: str, **params: Any) -> ValidationError:
    return ValidationError({name: [field_error(code, message_key=key, params=params or None)]})


def _check_customer_slot(
    professional: Professional, customer_id: int, start: datetime, duration_min: int, now: datetime
) -> datetime:
    """The ``start`` rows of the C§2.11 booking table, in order (after the customer lock and the count);
    returns the end of the slot.

    The end is computed only once the start is inside the horizon: a start at the edge of the datetime range
    (``9999-12-31T23:59…``) would overflow ``start + duration`` (or its conversion to Tashkent time) and is
    beyond the horizon anyway, so it answers ``out_of_range`` instead of a 500. A start at the other edge
    (``0001-01-01…``) is refused as ``in_past`` first.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValidationError({"start": [field_error("invalid")]})
    lead = int(settings.BOOKING_MIN_LEAD_MINUTES)
    if start < now + timedelta(minutes=lead):
        raise _field("start", "in_past", "field.booking_too_soon", n=lead)
    horizon_error = _field(
        "start", "out_of_range", "field.booking_horizon", n=int(settings.BOOKING_HORIZON_DAYS)
    )
    try:
        day = timeutils.local_date(start)
    except OverflowError:
        raise horizon_error from None
    if day > selectors.booking_horizon_end(now):
        raise horizon_error
    end = start + timedelta(minutes=duration_min)
    frame = selectors.day_frame(professional.pk, day)
    if frame.hours is None:
        raise _field("start", "outside_working_hours", "field.outside_working_hours")
    if not selectors.on_grid(professional, frame, start):
        raise _field("start", "invalid", "field.booking_grid", n=professional.slot_step_min)
    if start < frame.hours[0] or end > frame.hours[1]:
        raise _field("start", "outside_working_hours", "field.outside_working_hours")
    if selectors.overlaps_busy(frame, start, end):
        raise _field("start", "slot_unavailable", "field.slot_unavailable")
    if selectors.customer_open_overlap(customer_id, start, end):
        raise _field("start", "slot_unavailable", "field.own_overlap")
    return end


@transaction.atomic
def request_by_customer(
    customer_id: int,
    professional_id: int,
    service_id: int,
    start: datetime,
    note: str = "",
    *,
    request: Any = None,
) -> Appointment:
    """``POST appointments/`` (C§2.11): the checks of the booking table in order, then a ``requested``
    appointment (``requested_via=app``, duration and price snapshotted from the link, the customer's note).

    The customer row is locked first (``FOR NO KEY UPDATE``, C§2.7.1) before the open-appointment count and
    the own-overlap check, so concurrent bookings of one customer serialise; a concurrent booking of the slot
    by someone else that wins the ``appointment_no_overlap`` exclusion (or the 40P01 deadlock it can raise)
    answers 409 ``slot_taken`` with nothing written. A customer deleted (or blocked) while the booking waited
    on its lock answers 401 ``SESSION_EXPIRED`` (its sessions are revoked with it). Event ``requested``
    (source ``customer``) and audit ``appointment.requested`` (actor type ``customer``); no push.
    """
    professional = Professional.objects.filter(pk=professional_id, is_active=True).first()
    if professional is None:
        raise _field("specialist_id", "does_not_exist", "field.specialist_not_found")
    link = selectors.offered_link(professional.pk, service_id)
    if link is None:
        raise _field("service_id", "does_not_exist", "field.service_not_offered")
    customer = (
        Customer.objects.select_for_update(no_key=True)
        .filter(pk=customer_id, status=enums.CustomerStatus.ACTIVE)
        .first()
    )
    if customer is None:
        # ``DELETE me/`` (or a staff block) committed while this booking waited on the customer lock: the
        # re-checked row is no longer active and its sessions are revoked (C§2.2.3: "refused because the
        # customer is no longer active").
        raise AuthFailed("SESSION_EXPIRED")
    if not _hold_active_specialist(professional.pk):  # deactivated while this booking was being checked
        raise _field("specialist_id", "does_not_exist", "field.specialist_not_found")
    _check_consent(customer)
    now = timeutils.now()
    limit = int(settings.BOOKING_MAX_OPEN_PER_CUSTOMER)
    if selectors.customer_open_future_count(customer.pk, now) >= limit:
        raise BookingLimit(params={"limit": limit}, details={"limit": limit})
    end = _check_customer_slot(professional, customer.pk, start, link.duration_min, now)
    _before_write()
    try:
        with transaction.atomic():
            appointment = Appointment.objects.create(
                professional=professional,
                service_id=link.service_id,
                customer=customer,
                start=start,
                end=end,
                duration_min=link.duration_min,
                price=link.price,
                status=enums.AppointmentStatus.REQUESTED,
                requested_via=enums.RequestedVia.APP,
                requested_at=now,
                created_by=None,
                customer_note=note,
            )
    except (IntegrityError, OperationalError) as exc:  # appointment_no_overlap: a concurrent booking won
        if not _lost_the_slot(exc):
            raise
        raise SlotTaken() from exc
    record_event(
        appointment,
        AppointmentEvent.Type.REQUESTED,
        source=AppointmentEvent.Source.CUSTOMER,
        data={"via": enums.RequestedVia.APP, **_range_data(appointment)},
    )
    _audit(appointment, "requested", actor=customer, request=request, requested_via=enums.RequestedVia.APP)
    return appointment


@transaction.atomic
def cancel_by_customer(
    appointment_id: int,
    customer_id: int,
    *,
    reason: str = "",
    at: datetime | None = None,
    request: Any = None,
    account_deleted: bool | None = None,
) -> Appointment:
    """``POST appointments/{id}/cancel/`` (C§2.11) and ``DELETE me/`` step 3 (C§2.2.3).

    Under the row lock: an appointment of another customer → ``NotFound`` (404); allowed for ``requested`` /
    ``confirmed`` with ``start − now ≥ BOOKING_CANCEL_MIN_LEAD_MINUTES``, else 409 ``not_cancellable``
    (``error.appointment_not_cancellable``). The account deletion cancels every open **future** appointment
    without the lead rule (it protects the specialist's day from a customer's late cancel, not from a deleted
    account, and must not abort the deletion); an appointment that already started stays as it is.
    ``status=cancelled``, ``cancelled_by=customer``; event ``cancelled {by: "customer", reason, push_sent:
    false}`` (source ``customer``); the slot is free again; no push; audit ``appointment.cancelled`` (actor
    type ``customer``).

    ``account_deleted`` is a server-side flag: the customer route always passes ``False``, so the free-text
    ``reason`` a client sends never changes the rules. ``None`` (the C§2.2.3 call shape, ``reason =
    "account_deleted"``, used by ``customers.services.delete_account``) derives it from the reason.
    """
    if account_deleted is None:
        account_deleted = reason == ACCOUNT_DELETED_REASON
    instant = timeutils.now() if at is None else at
    appointment = (
        Appointment.objects.select_for_update().filter(pk=appointment_id, customer_id=customer_id).first()
    )
    if appointment is None:
        raise NotFound
    allowed = appointment.status in OPEN_STATUSES and (
        selectors.can_cancel(appointment, instant) or (account_deleted and appointment.start >= instant)
    )
    if not allowed:
        raise not_cancellable("appointment")
    appointment.status = enums.AppointmentStatus.CANCELLED
    appointment.cancelled_at = instant
    appointment.cancelled_by = Appointment.CancelledBy.CUSTOMER
    appointment.save(update_fields=["status", "cancelled_at", "cancelled_by", "updated_at"])
    record_event(
        appointment,
        AppointmentEvent.Type.CANCELLED,
        source=AppointmentEvent.Source.CUSTOMER,
        data={"by": "customer", "reason": reason or None, "push_sent": False},
    )
    customer = Customer.objects.filter(pk=customer_id).first()
    _audit(appointment, "cancelled", actor=customer, request=request, by="customer", reason=reason or None)
    return appointment
