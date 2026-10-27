"""Demo appointments (design 03 §7.11): the nine design rows and the frontend's generated surrounding days.

Rows 1–8 are ``appointments.ts DESIGN_APPOINTMENTS`` on Monday 26 October 2026, row 9 Malika's visit on the
27th; the surrounding days (offsets −3 … +4, Sundays skipped) come from ``createRandom(26)`` in the mock's
draw order (``surroundingDays()``), with ``core.seeding.MockRandom`` so the slots, services and customers
equal the frontend's. Every row is inserted directly with its ``AppointmentEvent`` trail: ``requested`` at
``requested_at``; a confirmed row is confirmed by the Administrator 30 minutes later through
``appointments.services.confirm`` (``confirmed`` event, ``push.appointment`` outbox message, audit entry;
actor staff ``admin@glowderma.uz``); a past generated row is ``attended`` at its end through
``services.attend``. Inbox rows are not written here — the outbox handler in ``notifications`` creates them
when the pushes are delivered.

Idempotent by ``(professional, start)``; design rows take their ids explicitly, generated rows take ids from
1000 (the sequence is parked at 999 first). A missing lower-layer row (professional, service, link, customer,
admin) skips the affected row or event; a generated slot that collides with a pinned one (exclusion
``IntegrityError``) is dropped. Every ``created_at``/``updated_at`` is pinned to a design instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.seeds import all_customers
from glowderma.appointments import services
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.core import enums, timeutils
from glowderma.core.seeding import MockRandom, SeedContext, pinned_clock, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import Professional, ProfessionalService, Service
from glowderma.professionals.seeds import PROFESSIONALS, SERVICES

SEED_ADMIN_EMAIL = "admin@glowderma.uz"
DESIGN_DAY = date(2026, 10, 26)
CONFIRM_DELAY = timedelta(minutes=30)
GENERATED_SEED = 26
GENERATED_ID_FLOOR = 999  # generated rows take ids from 1000 (design 03 §7.1)
SURROUNDING_OFFSETS = (-3, -2, -1, 1, 2, 3, 4)
SLOT_CHOICES = ("09:00", "10:00", "11:30", "14:30", "16:00")
GENERATED_REQUEST_CLOCK = time(18, 20)
GENERATED_CUSTOMER_IDS = range(13, 433)  # ``CUSTOMERS.slice(12)``: the 420 generated customers
CONSULTATION, FOLLOW_UP = 1, 2  # service ids of professionals.seed
COSMETOLOGY = (3, 4, 5)  # ``COSMETOLOGY`` in the mock, by service id


def _at(day: date, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


@dataclass(frozen=True, slots=True)
class AppointmentSpec:
    pk: int | None  # explicit id for the design rows, None for the generated ones
    professional_id: int  # design id (professionals.seed)
    customer_id: int  # design id (accounts.seed)
    service_id: int  # design id (professionals.seed)
    start: datetime
    price: int | None  # design price; None = the professional's link price
    status: str
    requested_at: datetime
    requested_via: str = enums.RequestedVia.APP


def _design(
    pk: int,
    professional_id: int,
    customer_id: int,
    service_id: int,
    day: date,
    clock: str,
    price: int,
    status: str,
    requested_at: datetime,
    requested_via: str = enums.RequestedVia.APP,
) -> AppointmentSpec:
    return AppointmentSpec(
        pk,
        professional_id,
        customer_id,
        service_id,
        _at(day, clock),
        price,
        status,
        requested_at,
        requested_via,
    )


def _dt(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TASHKENT)


REQUESTED, CONFIRMED, ATTENDED = (
    enums.AppointmentStatus.REQUESTED,
    enums.AppointmentStatus.CONFIRMED,
    enums.AppointmentStatus.ATTENDED,
)
DESIGN_APPOINTMENTS: tuple[AppointmentSpec, ...] = (
    _design(1, 1, 8, 1, DESIGN_DAY, "09:00", 150_000, REQUESTED, _dt(10, 24, 8, 41)),
    _design(2, 1, 2, 2, DESIGN_DAY, "11:00", 100_000, CONFIRMED, _dt(10, 21, 19, 12)),
    _design(
        3, 1, 11, 1, DESIGN_DAY, "14:00", 150_000, CONFIRMED, _dt(10, 22, 10, 5), enums.RequestedVia.ADMIN
    ),
    _design(4, 2, 4, 1, DESIGN_DAY, "09:30", 120_000, REQUESTED, _dt(10, 24, 7, 58)),
    _design(5, 2, 6, 2, DESIGN_DAY, "15:00", 90_000, CONFIRMED, _dt(10, 20, 12, 40)),
    _design(6, 3, 3, 3, DESIGN_DAY, "10:00", 320_000, CONFIRMED, _dt(10, 19, 16, 22)),
    _design(7, 3, 10, 4, DESIGN_DAY, "12:00", 380_000, REQUESTED, _dt(10, 23, 21, 3)),
    _design(8, 3, 12, 5, DESIGN_DAY, "15:30", 260_000, CONFIRMED, _dt(10, 22, 9, 47)),
    _design(9, 3, 5, 5, date(2026, 10, 27), "10:00", 260_000, CONFIRMED, _dt(10, 22, 14, 10)),
)


def generated_appointments() -> list[AppointmentSpec]:
    """``surroundingDays()`` of ``appointments.ts``: same draws in the same order, so the same rows."""
    random = MockRandom(GENERATED_SEED)
    result: list[AppointmentSpec] = []
    for offset in SURROUNDING_OFFSETS:
        day = DESIGN_DAY + timedelta(days=offset)
        if day.weekday() == 6:  # closed on Sundays
            continue
        past = offset < 0
        requested_at = datetime.combine(day - timedelta(days=3), GENERATED_REQUEST_CLOCK, tzinfo=TASHKENT)
        for column, professional_id in enumerate((1, 2, 3)):
            slots = random.shuffle(SLOT_CHOICES)[: random.int(1, 3)]
            for clock in sorted(slots):
                cosmetic = random.pick(COSMETOLOGY) if column == 2 else None
                if cosmetic is not None:
                    service_id = cosmetic
                else:
                    service_id = CONSULTATION if random.chance(0.6) else FOLLOW_UP
                customer_id = random.pick(GENERATED_CUSTOMER_IDS)
                result.append(
                    AppointmentSpec(
                        None,
                        professional_id,
                        customer_id,
                        service_id,
                        _at(day, clock),
                        None,
                        ATTENDED if past else CONFIRMED,
                        requested_at,
                    )
                )
    return result


class _Lookup:
    """Design ids → rows by natural key; a missing row reads as ``None`` (standalone runs)."""

    def __init__(self) -> None:
        # the demo staff (id 1 of accounts.seed) through get_user_model(), as every seeder does
        self.admin = get_user_model().objects.filter(email=SEED_ADMIN_EMAIL).first()
        by_name = {
            p.name: p for p in Professional.objects.filter(name__in=[name for _, name, _ in PROFESSIONALS])
        }
        self.professionals = {pk: by_name.get(name) for pk, name, _ in PROFESSIONALS}
        by_name_uz = {s.name_uz: s for s in Service.objects.filter(name_uz__in=[row[1] for row in SERVICES])}
        self.services = {pk: by_name_uz.get(name_uz) for pk, name_uz, *_ in SERVICES}
        self.phones = {pk: phone for pk, _, phone in all_customers()}
        self._customers: dict[int, Customer | None] = {}
        self.links = {
            (link.professional_id, link.service_id): link
            for link in ProfessionalService.objects.filter(
                professional__in=[p for p in self.professionals.values() if p is not None]
            )
        }

    def customer(self, design_id: int) -> Customer | None:
        if design_id not in self._customers:
            phone = self.phones.get(design_id)
            self._customers[design_id] = (
                None if phone is None else Customer.objects.filter(phone=phone).first()
            )
        return self._customers[design_id]


def _event(
    appointment: Appointment,
    type_: str,
    source: str,
    actor: StaffUser | None,
    data: dict[str, object],
    at: datetime,
) -> None:
    with pinned_clock(at):
        AppointmentEvent.objects.create(
            appointment=appointment, type=type_, source=source, actor=actor, data=data
        )


def _insert(spec: AppointmentSpec, lookup: _Lookup) -> bool:
    """Insert one appointment with its events; ``False`` when a lower-layer row is missing."""
    professional = lookup.professionals.get(spec.professional_id)
    service = lookup.services.get(spec.service_id)
    customer = lookup.customer(spec.customer_id)
    if professional is None or service is None or customer is None:
        return False
    link = lookup.links.get((professional.pk, service.pk))
    if link is None:
        return False
    by_admin = spec.requested_via == enums.RequestedVia.ADMIN
    admin = lookup.admin
    if by_admin and admin is None:
        return False  # appointment_created_by_via needs the actor
    price = link.price if spec.price is None else spec.price
    duration = link.duration_min
    end = spec.start + timedelta(minutes=duration)
    with pinned_clock(spec.requested_at):
        appointment = Appointment(
            professional=professional,
            service=service,
            customer=customer,
            start=spec.start,
            end=end,
            duration_min=duration,
            price=price,
            status=REQUESTED,
            requested_via=spec.requested_via,
            requested_at=spec.requested_at,
            created_by=admin if by_admin else None,
        )
        if spec.pk is not None:
            appointment.pk = spec.pk
        appointment.save(force_insert=True)
    _event(
        appointment,
        AppointmentEvent.Type.REQUESTED,
        AppointmentEvent.Source.ADMIN if by_admin else AppointmentEvent.Source.CUSTOMER,
        admin if by_admin else None,
        {
            "via": spec.requested_via,
            "start": timeutils.isoformat(spec.start),
            "end": timeutils.isoformat(end),
            "service_id": service.pk,
            "professional_id": professional.pk,
            "price": price,
            "duration_min": duration,
        },
        spec.requested_at,
    )
    if spec.status == REQUESTED:
        return True
    confirmed_at = spec.requested_at + CONFIRM_DELAY
    if admin is None:
        # apptevent_source_by_type: a confirmation is always an admin event, so without the demo staff the
        # row keeps its design status and no event (or push) is written.
        appointment.status = spec.status
        appointment.confirmed_at = confirmed_at
        appointment.attended_at = end if spec.status == ATTENDED else None
        with pinned_clock(end if spec.status == ATTENDED else confirmed_at):
            appointment.save(update_fields=["status", "confirmed_at", "attended_at"])
        return True
    # The replayed flow (design 03 §7.11): the Administrator confirms 30 minutes after the request through the
    # service, which writes the event, enqueues the push (the inbox row comes from the outbox handler) and
    # audits; a past generated row is then attended at its end.
    with pinned_clock(confirmed_at):
        services.confirm(appointment.pk, actor=admin)
    if spec.status == ATTENDED:
        with pinned_clock(end):
            services.attend(appointment.pk, actor=admin)
    return True


def _park_sequence(floor: int) -> None:
    """Move the appointment id sequence past the explicit design ids (and ``floor``)."""
    table = Appointment._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
        row = cursor.fetchone()
        if row is None or not row[0]:
            return
        cursor.execute(f'SELECT GREATEST(COALESCE(MAX("id"), 0), %s) FROM "{table}"', [floor])  # noqa: S608
        value = cursor.fetchone()
        cursor.execute("SELECT setval(%s, %s, true)", [row[0], int(value[0]) if value else floor])


def _seed_rows(specs: list[AppointmentSpec], lookup: _Lookup) -> tuple[int, int]:
    """(inserted, dropped) — dropped = overlap with an existing open slot (exclusion constraint)."""
    inserted = dropped = 0
    for spec in specs:
        professional = lookup.professionals.get(spec.professional_id)
        if professional is None:
            continue
        if Appointment.objects.filter(professional=professional, start=spec.start).exists():
            continue
        try:
            with transaction.atomic():
                if _insert(spec, lookup):
                    inserted += 1
        except IntegrityError:
            dropped += 1
    return inserted, dropped


@register_seeder("appointments", order=80)
def seed_appointments(context: SeedContext) -> None:
    with transaction.atomic():
        lookup = _Lookup()
        _seed_rows(list(DESIGN_APPOINTMENTS), lookup)
        _park_sequence(GENERATED_ID_FLOOR)
        _seed_rows(generated_appointments(), lookup)
