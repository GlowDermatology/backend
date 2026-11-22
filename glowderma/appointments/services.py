"""Writes of `appointments` (design 03 §2.1 state machine; T10 admin creation / rescheduling / cancellation).

Every transition runs under ``select_for_update()`` of the row, writes one ``AppointmentEvent`` through
:func:`record_event`, enqueues the ``push.appointment`` outbox message where the design says so and audits
``appointment.<action>``. Slot rules live in :func:`check_slot`; the ``btree_gist`` exclusion constraint is
the last line of defence against a concurrent booking (``IntegrityError`` → 409 ``conflict``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from glowderma.accounts import services as account_services
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
from glowderma.core.uzsource import N_
from glowderma.professionals.models import Location, Professional, Service
from glowderma.professionals.selectors import (
    DAY_END_LABEL,
    active_specialist_account,
    day_schedules,
    has_location,
)
from glowderma.site_config import selectors as site_config_selectors

SLOT_STEP_MIN = 5  # admin bookings sit on a 5-minute grid (open point A-4: not the professional's slot_step)
MESSAGES = {
    "invalid_transition": N_("Bu amal ushbu holatda mumkin emas."),
    "slot_taken": N_("Bu vaqt band."),
    "outside_working_hours": N_("Mutaxassis bu vaqtda ishlamaydi."),
    "in_past": N_("Kelajakdagi vaqtni tanlang."),
    "grid": N_("Vaqt 5 daqiqalik qadamda bo‘lsin."),
    "service_not_offered": N_("Mutaxassis bu xizmatni ko‘rsatmaydi."),
    "specialist_not_found": N_("Mutaxassis topilmadi."),
    "service_not_found": N_("Xizmat topilmadi."),
    "customer_inactive": N_("Bu raqamdagi mijoz hisobi faol emas."),
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
    professional: Professional,
    start: datetime,
    end: datetime,
    *,
    location_id: int | None = None,
    exclude_pk: int | None = None,
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
    if location_id is not None and (schedule.location is None or schedule.location.pk != location_id):
        raise _wrong_visit_location()
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


def _hold_row(model: type[Professional] | type[Service] | type[Location], pk: int) -> bool:
    """Lock a row ``FOR KEY SHARE`` for the rest of the transaction and re-read its ``is_active``."""
    table = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
        # The table name comes from the model's metadata, the id is a bound parameter.
        cursor.execute(f"SELECT is_active FROM {table} WHERE id = %s FOR KEY SHARE", [pk])  # noqa: S608
        row = cursor.fetchone()
    return bool(row and row[0])


def _hold_active_service(service_id: int) -> bool:
    """Lock the service's row ``FOR KEY SHARE`` and re-read ``is_active``: a service write (``PATCH
    …/services/{id}/``, after the specialist) takes the row ``FOR UPDATE``, so a booking either waits for a
    deactivation and then sees it (refused), or the deactivation waits for the booking (which is then kept,
    PS-3)."""
    return _hold_row(Service, service_id)


def _hold_active_specialist(professional_id: int) -> bool:
    """Lock the specialist's row ``FOR KEY SHARE`` for the rest of the transaction and re-read ``is_active``.

    The deactivation of the admin specialists API counts the open appointments under ``SELECT … FOR UPDATE``
    of the same row, which conflicts with this lock (bookings among themselves do not: key-share locks are
    compatible). Either the deactivation waits for this booking and then counts it (409 unless ``force``),
    or this booking waits for the deactivation and then reads ``is_active = false`` (refused). The foreign
    key of the new appointment cannot serve: Django creates it ``DEFERRABLE INITIALLY DEFERRED``, so its
    check runs only at commit, after the guard.

    Every schedule and service write of the admin and specialist APIs (hours, overrides, blocks, the
    specialist's services) holds the same row ``FOR UPDATE``, so whatever a booking reads *after* this lock
    (the service, its price and duration, the day's hours and blocks) is the committed state: the bookings
    therefore read the service and check the slot only once they hold it.
    """
    return _hold_row(Professional, professional_id)


def _visit_location(professional: Professional, location_id: int | None) -> int:
    # Archive locks the same row before checking upcoming visits. ``None``: a draft without a clinic.
    if (
        location_id is None
        or not _hold_row(Location, location_id)
        or not has_location(professional, location_id)
    ):
        raise _field("location_id", "does_not_exist", "field.booking_location_not_assigned")
    return location_id


def _wrong_visit_location() -> ValidationError:
    return _field("location_id", "invalid", "field.booking_wrong_location")


def _specialist_gone() -> ValidationError:
    return ValidationError(
        {"specialist_id": [field_error("does_not_exist", MESSAGES["specialist_not_found"])]}
    )


def _service_not_offered() -> ValidationError:
    return ValidationError({"service_id": [field_error("does_not_exist", MESSAGES["service_not_offered"])]})


def owned_active_service(professional: Professional, service: Service) -> Service:
    """``service`` re-read under the caller's locks (specialist, then service): it must belong to
    ``professional`` and be active, else 400 ``service_id``. Its price and duration are the snapshot."""
    service.refresh_from_db(fields=["professional", "price", "duration_min", "is_active"])
    if service.professional_id != professional.pk or not service.is_active:
        raise _service_not_offered()
    return service


@dataclass(frozen=True, slots=True)
class WalkIn:
    """A booking for a phone no customer holds yet (the staff booking dialog): the customer is created with
    the booking, without a password or consent. They sign in later with an SMS code to that phone into this
    same row (``auth/send-code/`` ``reset_password`` then ``auth/reset-password/``), which sets the password,
    and accept the terms through ``me/consent/`` (``me/`` reports the consent as ``update_required``)."""

    phone: str
    name: str


def customer_for_booking(customer: Customer | WalkIn, *, actor: Any, request: Any = None) -> Customer:
    """The booked customer: ``customer`` itself, or the walk-in created now (Uzbek, the default language) and
    audited ``customer.created_by_staff``. A concurrent registration of the phone wins: its active row is
    used; a phone held by a blocked or deleted account is 400 ``phone``."""
    if isinstance(customer, Customer):
        return customer
    try:
        created = account_services.create_customer(
            customer.phone, name=customer.name, language=enums.ContentLanguage.UZ
        )
    except IntegrityError:
        existing = account_services.active_customer_by_phone(customer.phone)
        if existing is None:
            raise ValidationError(
                {"phone": [field_error("customer_not_found", MESSAGES["customer_inactive"])]}
            ) from None
        return existing
    audit.record(
        action="customer.created_by_staff",
        entity_type="customer",
        entity_id=str(created.pk),
        actor=actor,
        request=request,
        after={"source": "appointment"},
    )
    return created


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
            "location_id": appointment.location_id,
            "start": timeutils.isoformat(appointment.start),
            "end": timeutils.isoformat(appointment.end),
            **extra,
        },
    )


def _locked(appointment_id: int) -> Appointment:
    return Appointment.objects.select_for_update().get(pk=appointment_id)


def _locked_for_action(appointment_id: int, action: str, specialist_account_id: int | None) -> Appointment:
    if specialist_account_id is None:
        return _locked(appointment_id)
    appointment = (
        Appointment.objects.select_for_update(of=("self",))
        .filter(pk=appointment_id, professional__staff_user_id=specialist_account_id)
        .first()
    )
    if appointment is None:
        raise NotFound
    professional = active_specialist_account(specialist_account_id, lock=True)
    if professional is None:
        raise PermissionDenied
    if professional.pk != appointment.professional_id:
        raise NotFound
    started = appointment.start <= timeutils.now()
    if started != (action == enums.AppointmentAction.ATTEND):
        raise InvalidTransition(N_("Bu amalni qabulning ushbu vaqtida bajarib bo‘lmaydi."))
    return appointment


def _range_data(appointment: Appointment) -> dict[str, Any]:
    return {
        "start": timeutils.isoformat(appointment.start),
        "end": timeutils.isoformat(appointment.end),
        "service_id": appointment.service_id,
        "location_id": appointment.location_id,
        "professional_id": appointment.professional_id,
        "price": appointment.price,
        "duration_min": appointment.duration_min,
    }


# --- admin transitions (contract §2.9) --------------------------------------------------------------------


@transaction.atomic
def transition(
    appointment_id: int,
    action: str,
    *,
    actor: Any,
    request: Any = None,
    specialist_account_id: int | None = None,
) -> Appointment:
    """``confirm`` / ``reject`` / ``attend``; any other source status → 409 ``invalid_transition``."""
    allowed_from, target = TRANSITIONS[action]
    appointment = _locked_for_action(appointment_id, action, specialist_account_id)
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
    customer: Customer | WalkIn,
    start: datetime,
    location_id: int | None = None,
    note: str = "",
    actor: Any,
    request: Any = None,
) -> Appointment:
    """A confirmed booking made by staff: ``requested_via = admin``, events ``requested`` + ``confirmed``; a
    :class:`WalkIn` customer is created in the same transaction (nothing is kept when the booking fails).

    The specialist and the service are locked first (``FOR KEY SHARE``); the service, the snapshot and the
    slot are read under those locks, so a concurrent schedule or service change is either seen or waits for
    this booking."""
    try:
        with transaction.atomic():
            if not _hold_active_specialist(professional.pk):  # deactivated since the serializer read it
                raise _specialist_gone()
            if not _hold_active_service(service.pk):  # retired since the serializer read it
                raise ValidationError(
                    {"service_id": [field_error("does_not_exist", MESSAGES["service_not_found"])]}
                )
            owned_active_service(professional, service)
            professional.refresh_from_db(fields=["location"])
            clinic_id = _visit_location(
                professional, professional.location_id if location_id is None else location_id
            )
            end = start + timedelta(minutes=service.duration_min)
            check_slot(professional, start, end, location_id=clinic_id)
            booked = customer_for_booking(customer, actor=actor, request=request)
            _before_write()
            now = timeutils.now()
            appointment = Appointment.objects.create(
                professional=professional,
                location_id=clinic_id,
                service=service,
                customer=booked,
                start=start,
                end=end,
                duration_min=service.duration_min,
                price=service.price,
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
    service: Service | None = None,
    location_id: int | None = None,
    actor: Any,
    request: Any = None,
    specialist_account_id: int | None = None,
    customer_agreed: bool = False,
) -> Appointment:
    """Move an open appointment (status unchanged). Every service belongs to one specialist, so a move to
    another specialist needs ``service``, one of that specialist's active services (400 ``service_id``
    otherwise); a changed service re-snapshots duration and price from it. The booked service kept keeps the
    booked duration and price (even if it was deactivated since)."""
    appointment = _locked_for_action(appointment_id, "reschedule", specialist_account_id)
    if appointment.status not in OPEN_STATUSES:
        raise InvalidTransition(MESSAGES["invalid_transition"])
    target = professional or appointment.professional
    if specialist_account_id is not None and target.pk != appointment.professional_id:
        raise PermissionDenied
    duration, price = appointment.duration_min, appointment.price
    # Always lock the target specialist before the slot check, so a concurrent hours / override / block
    # change is seen or waits. The same (possibly deactivated, PS-2) specialist keeps its appointment
    # movable; a different one must be active.
    target_active = _hold_active_specialist(target.pk)
    if target.pk != appointment.professional_id and not target_active:  # deactivated since the serializer
        raise _specialist_gone()
    new_service = appointment.service if service is None else service
    if new_service.professional_id != target.pk:
        raise _service_not_offered()
    service_changed = new_service.pk != appointment.service_id
    if service_changed:
        _hold_active_service(new_service.pk)
        owned_active_service(target, new_service)
        duration, price = new_service.duration_min, new_service.price
    target.refresh_from_db(fields=["location"])
    default_location_id = (
        appointment.location_id if target.pk == appointment.professional_id else target.location_id
    )
    clinic_id = _visit_location(target, default_location_id if location_id is None else location_id)
    specialist_clinic_move = specialist_account_id is not None and clinic_id != appointment.location_id
    if specialist_clinic_move and not customer_agreed:
        raise ValidationError(
            {
                "customer_agreed": [
                    field_error("required", N_("Mijoz yangi filialga rozi bo‘lganini tasdiqlang."))
                ]
            }
        )
    agreement = {"customer_agreed": True} if specialist_clinic_move else {}
    end = start + timedelta(minutes=duration)
    check_slot(target, start, end, location_id=clinic_id, exclude_pk=appointment.pk)
    _before_write()
    before = {
        "from_start": timeutils.isoformat(appointment.start),
        "from_end": timeutils.isoformat(appointment.end),
        "from_location_id": appointment.location_id,
    }
    from_professional = appointment.professional_id
    from_service = appointment.service_id
    appointment.professional = target
    appointment.service = new_service
    appointment.location_id = clinic_id
    appointment.start, appointment.end = start, end
    appointment.duration_min, appointment.price = duration, price
    appointment.reschedule_count += 1
    try:
        with transaction.atomic():
            appointment.save(
                update_fields=[
                    "professional",
                    "service",
                    "location",
                    "start",
                    "end",
                    "duration_min",
                    "price",
                    "reschedule_count",
                ]
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
            "to_location_id": clinic_id,
            **({"from_service_id": from_service, "to_service_id": new_service.pk} if service_changed else {}),
            **agreement,
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
        to_location_id=clinic_id,
        reschedule_no=appointment.reschedule_count,
        **agreement,
    )
    return appointment


@transaction.atomic
def cancel(
    appointment_id: int,
    *,
    reason: str = "",
    actor: Any,
    request: Any = None,
    specialist_account_id: int | None = None,
) -> Appointment:
    """Cancel an open appointment, free the slot and notify the customer."""
    appointment = _locked_for_action(appointment_id, "cancel", specialist_account_id)
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
    professional: Professional,
    customer_id: int,
    start: datetime,
    duration_min: int,
    now: datetime,
    location_id: int,
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
    if frame.location_id != location_id:
        raise _wrong_visit_location()
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
    location_id: int | None = None,
    request: Any = None,
) -> Appointment:
    """``POST appointments/`` (C§2.11): the checks of the booking table in order, then a ``requested``
    appointment (``requested_via=app``, duration and price snapshotted from the service, the customer's
    note).

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
    if selectors.offered_service(professional.pk, service_id) is None:
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
    # Re-read the service under the specialist and service locks: a service write (specialist row, then
    # service row ``FOR UPDATE``) that committed meanwhile is seen here — a retired service is refused, a new
    # price / duration is snapshotted.
    offer = (
        selectors.offered_service(professional.pk, service_id) if _hold_active_service(service_id) else None
    )
    if offer is None:
        raise _field("service_id", "does_not_exist", "field.service_not_offered")
    professional.refresh_from_db(fields=["location", "slot_step_min"])
    clinic_id = _visit_location(
        professional, professional.location_id if location_id is None else location_id
    )
    _check_consent(customer)
    now = timeutils.now()
    limit = int(settings.BOOKING_MAX_OPEN_PER_CUSTOMER)
    if selectors.customer_open_future_count(customer.pk, now) >= limit:
        raise BookingLimit(params={"limit": limit}, details={"limit": limit})
    end = _check_customer_slot(professional, customer.pk, start, offer.duration_min, now, clinic_id)
    _before_write()
    try:
        with transaction.atomic():
            appointment = Appointment.objects.create(
                professional=professional,
                location_id=clinic_id,
                service_id=offer.pk,
                customer=customer,
                start=start,
                end=end,
                duration_min=offer.duration_min,
                price=offer.price,
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
