"""appointments.seed (design 03 §7.11): design rows, the frontend-identical generated days, idempotency,
standalone runs, pinned timestamps and the pending summary at the anchor."""

import importlib
from datetime import date, datetime, timedelta

import pytest
from django.db import models

from glowderma.accounts import seeds as accounts_seeds
from glowderma.accounts.models import Customer, Device, StaffUser
from glowderma.appointments import seeds
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.seeds import generated_appointments, seed_appointments
from glowderma.core import enums, seeding, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import SeedContext
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import seeds as professionals_seeds
from glowderma.professionals.models import Professional

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)

pytestmark = pytest.mark.django_db


def at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}:00+05:00").astimezone(TASHKENT)


def seed_lower_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    accounts_seeds.seed_accounts(CONTEXT)
    professionals_seeds.seed_professionals(CONTEXT)


def test_seeder_registered_with_order_80(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    importlib.reload(seeds)
    assert [(name, order) for name, order, _ in seeding.registered_seeders()] == [("appointments", 80)]


def test_generated_days_equal_the_frontend_vector() -> None:
    """``surroundingDays()`` run in node (``createRandom(26)``): day, specialist, start, service, customer."""
    rows = [
        (
            r.start.date().isoformat(),
            r.professional_id,
            f"{r.start:%H:%M}",
            r.service_id,
            r.customer_id,
            r.status,
        )  # noqa: E501
        for r in generated_appointments()
    ]
    assert len(rows) == 36
    assert rows[0] == ("2026-10-23", 1, "09:00", 1, 428, "attended")
    assert rows[1] == ("2026-10-23", 1, "10:00", 2, 361, "attended")
    assert rows[4] == ("2026-10-23", 3, "11:30", 5, 248, "attended")
    assert rows[5] == ("2026-10-23", 3, "16:00", 3, 305, "attended")
    assert rows[10] == ("2026-10-24", 3, "09:00", 3, 347, "attended")
    assert rows[11] == ("2026-10-27", 1, "14:30", 2, 228, "confirmed")
    assert rows[17] == ("2026-10-27", 3, "16:00", 4, 207, "confirmed")
    assert rows[29] == ("2026-10-29", 3, "11:30", 4, 40, "confirmed")
    assert rows[35] == ("2026-10-30", 3, "14:30", 5, 237, "confirmed")
    assert {r[0] for r in rows} == {
        "2026-10-23",
        "2026-10-24",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
    }
    assert all(
        r.requested_at == at((r.start.date() - timedelta(days=3)).isoformat(), "18:20")
        for r in generated_appointments()
    )
    assert all(13 <= r.customer_id <= 432 for r in generated_appointments())


def test_seed_reproduces_the_design_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_lower_layers(monkeypatch)
    seed_appointments(CONTEXT)
    first = (Appointment.objects.count(), AppointmentEvent.objects.count())
    assert first == (
        9 + 36,
        45 + 42 + 11,
    )  # requested ×45; confirmed ×(6 design + 36 generated); attended ×11
    seed_appointments(CONTEXT)
    assert (Appointment.objects.count(), AppointmentEvent.objects.count()) == first

    rows = {row.pk: row for row in Appointment.objects.filter(pk__lte=9)}
    assert sorted(rows) == list(range(1, 10))
    kamola = rows[1]
    assert (kamola.professional_id, kamola.customer_id, kamola.service_id) == (1, 8, 1)
    assert kamola.start == at("2026-10-26", "09:00") and kamola.end == at("2026-10-26", "09:30")
    assert (kamola.duration_min, kamola.price, kamola.status) == (30, 150_000, "requested")
    assert kamola.requested_at == at("2026-10-24", "08:41") and kamola.requested_via == "app"
    assert kamola.created_at == kamola.updated_at == kamola.requested_at
    sardor = rows[3]
    assert sardor.requested_via == "admin" and sardor.created_by == StaffUser.objects.get(pk=1)
    assert sardor.status == "confirmed" and sardor.confirmed_at == at("2026-10-22", "10:35")
    assert sardor.created_at == sardor.requested_at and sardor.updated_at == sardor.confirmed_at
    bekzod = rows[4]
    assert (bekzod.professional_id, bekzod.price, bekzod.status) == (2, 120_000, "requested")
    assert bekzod.start == at("2026-10-26", "09:30")
    gulnora = rows[6]
    assert (gulnora.service_id, gulnora.duration_min, gulnora.price) == (3, 75, 320_000)
    assert gulnora.end == at("2026-10-26", "11:15")
    malika = rows[9]
    assert (
        malika.customer_id == 5 and malika.start == at("2026-10-27", "10:00") and malika.status == "confirmed"
    )
    assert malika.confirmed_at == at("2026-10-22", "14:40")
    # ordered by start: 09:00 a1, 09:30 a4, 10:00 a6, 11:00 a2, 12:00 a7, 14:00 a3, 15:00 a5, 15:30 a8
    statuses = list(Appointment.objects.filter(pk__lte=8).values_list("status", flat=True))
    assert statuses == ["requested", "requested", "confirmed", "confirmed", "requested", "confirmed", "confirmed", "confirmed"]  # fmt: skip  # noqa: E501

    # events: requested at requested_at (customer source, admin for row 3); confirmed 30 min later by staff 1
    events = {(e.type, e.source) for e in kamola.events.all()}
    assert events == {("requested", "customer")}
    requested = kamola.events.get()
    assert requested.at == kamola.requested_at and requested.actor is None
    assert requested.data == {
        "via": "app",
        "start": "2026-10-26T09:00:00+05:00",
        "end": "2026-10-26T09:30:00+05:00",
        "service_id": 1,
        "professional_id": 1,
        "price": 150_000,
        "duration_min": 30,
    }
    sardor_events = list(sardor.events.order_by("at"))
    assert [(e.type, e.source, e.actor_id) for e in sardor_events] == [
        ("requested", "admin", 1),
        ("confirmed", "admin", 1),
    ]
    assert sardor_events[0].data["via"] == "admin"
    pushable = Device.objects.filter(customer_id=11, is_active=True, push_enabled=True).exists()
    assert sardor_events[1].at == sardor.confirmed_at and sardor_events[1].data == {"push_sent": pushable}
    # the replayed confirm enqueued the push (design 03 §7.11: the inbox row comes from the outbox handler)
    assert OutboxMessage.objects.filter(dedupe_key="push.appointment:3:confirmed").exists()
    assert OutboxMessage.objects.filter(topic="push.appointment").count() == 6 + 36

    # generated rows: ids from 1000, past days attended, future days confirmed, link snapshots
    generated = Appointment.objects.filter(pk__gte=1000).order_by("pk")
    assert generated.count() == 36 and generated.first().pk == 1000  # type: ignore[union-attr]
    past = generated.filter(start__lt=ANCHOR)
    assert past.count() == 11 and set(past.values_list("status", flat=True)) == {"attended"}
    assert set(generated.filter(start__gt=ANCHOR).values_list("status", flat=True)) == {"confirmed"}
    first_generated = generated.first()
    assert first_generated is not None
    assert first_generated.start == at("2026-10-23", "09:00") and first_generated.customer_id == 428
    assert first_generated.requested_at == at("2026-10-20", "18:20")
    assert first_generated.attended_at == first_generated.end == at("2026-10-23", "09:30")
    assert first_generated.updated_at == first_generated.attended_at
    assert [(e.type, e.source) for e in first_generated.events.order_by("at")] == [
        ("requested", "customer"),
        ("confirmed", "admin"),
        ("attended", "admin"),
    ]
    jasur = generated.get(start=at("2026-10-27", "10:00"), professional_id=2)
    assert (jasur.service_id, jasur.price, jasur.duration_min) == (1, 120_000, 30)  # the link's snapshot
    assert not Appointment.objects.filter(start__week_day=1).exists()  # Sundays are skipped
    assert not Appointment.objects.filter(customer_id__lt=13, pk__gte=1000).exists()

    # every bookkeeping timestamp is pinned (design 03 §7.1): nothing carries the real clock
    assert not Appointment.objects.filter(updated_at__lt=models.F("created_at")).exists()
    assert not Appointment.objects.filter(created_at__gt=at("2026-10-30", "23:59")).exists()
    assert not AppointmentEvent.objects.filter(at__gt=at("2026-10-30", "23:59")).exists()
    assert not Appointment.objects.filter(created_at__gt=ANCHOR, start__lt=ANCHOR).exists()


def test_pending_summary_at_the_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """``{count 3, next_at 2026-10-26T09:00:00+05:00}`` — contract §2.9 over the seeded rows (plain ORM)."""
    seed_lower_layers(monkeypatch)
    seed_appointments(CONTEXT)
    pending = Appointment.objects.filter(status=enums.AppointmentStatus.REQUESTED, start__gte=ANCHOR)
    summary = pending.aggregate(count=models.Count("id"), next_at=models.Min("start"))
    assert summary == {"count": 3, "next_at": at("2026-10-26", "09:00")}
    assert timeutils.isoformat(summary["next_at"]) == "2026-10-26T09:00:00+05:00"
    upcoming = Appointment.objects.filter(
        customer_id=5, status__in=["requested", "confirmed"], start__gte=ANCHOR
    )
    assert upcoming.count() == 1  # Malika's card: upcoming_appointments = 1
    design_day = Appointment.objects.filter(start__date=date(2026, 10, 26))
    assert design_day.count() == 8


def test_seed_runs_standalone_without_lower_layers() -> None:
    """No professionals, customers or admin: every row is skipped and nothing fails."""
    seed_appointments(CONTEXT)
    assert Appointment.objects.count() == 0 and AppointmentEvent.objects.count() == 0


def test_seed_without_admin_skips_admin_rows_and_events(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_lower_layers(monkeypatch)
    StaffUser.objects.filter(email=seeds.SEED_ADMIN_EMAIL).update(email="someone-else@glowderma.uz")
    seed_appointments(CONTEXT)
    assert not Appointment.objects.filter(pk=3).exists()  # created_by is required for an admin booking
    assert Appointment.objects.count() == 8 + 36
    assert set(AppointmentEvent.objects.values_list("type", flat=True)) == {"requested"}
    assert Appointment.objects.get(pk=2).status == "confirmed"  # the row keeps its status


def test_seed_without_customers_skips_their_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    professionals_seeds.seed_professionals(CONTEXT)
    accounts_seeds.ensure_admin()
    seed_appointments(CONTEXT)
    assert Appointment.objects.count() == 0
    Customer.objects.create(id=8, phone="+998909741823", name="Kamola Nazarova")
    seed_appointments(CONTEXT)
    assert list(Appointment.objects.values_list("pk", flat=True)) == [1]


def test_generated_slot_colliding_with_a_pinned_row_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_lower_layers(monkeypatch)
    dilnoza = Professional.objects.get(pk=3)
    requested_at = at("2026-10-22", "15:00")
    extra = seeds._design(10, 3, 7, 5, date(2026, 10, 27), "08:30", 260_000, "confirmed", requested_at)
    monkeypatch.setattr(
        seeds,
        "DESIGN_APPOINTMENTS",
        (*seeds.DESIGN_APPOINTMENTS, extra),
    )
    seed_appointments(CONTEXT)
    assert Appointment.objects.filter(pk=10, professional=dilnoza).exists()
    # the generated Dilnoza 09:00 slot (60 min) on the 27th overlaps the pinned 08:30–09:30 row: dropped
    assert not Appointment.objects.filter(professional=dilnoza, start=at("2026-10-27", "09:00")).exists()
    assert Appointment.objects.filter(pk=9).exists()
    assert Appointment.objects.count() == 10 + 35
