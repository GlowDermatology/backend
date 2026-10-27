"""appointments models (design 03 §2, §10): every CHECK, the exclusion constraint, the raw duration check,
the indexes in pg_catalog, the append-only trail and its trigger (purge GUC refused), on_delete behaviour."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import ProtectedError

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.tests.factories import AppointmentEventFactory, AppointmentFactory
from glowderma.core import enums
from glowderma.core.db import purge_window
from glowderma.core.models import AppendOnlyError
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.tests.factories import ProfessionalFactory

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 10, 26, 9, 0, tzinfo=TASHKENT)
NOW = datetime(2026, 10, 24, 12, 5, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraints(table: str, kind: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = %s",
            [table, kind],
        )
        return dict(cursor.fetchall())


# --- Appointment --------------------------------------------------------------------------------------------


def test_appointment_defaults_str_and_ordering() -> None:
    later = AppointmentFactory.create(start=T0 + timedelta(hours=2))
    first = AppointmentFactory.create(start=T0)
    second = AppointmentFactory.create(start=T0)
    assert list(Appointment.objects.all()) == [first, second, later]  # start, id
    assert first.status == enums.AppointmentStatus.REQUESTED
    assert first.reschedule_count == 0 and first.cancelled_by == "" and first.created_by is None
    assert first.customer_note == "" and first.staff_note == "" and first.confirmed_at is None
    assert (
        str(first)
        == f"Appointment #{first.pk} requested: prof {first.professional_id} 2026-10-26 09:00 (30 min)"
    )
    assert first.end == T0 + timedelta(minutes=30)


def test_appointment_time_and_money_checks() -> None:
    # end > start is implied by the duration checks; PostgreSQL reports constraints in name order, so the
    # raw duration check fires first — the CHECK itself is asserted through pg_catalog below
    violates("appointment_(end_after_start|duration_matches)", AppointmentFactory.create, end=T0)
    violates(
        "appointment_(end_after_start|duration_matches)",
        AppointmentFactory.create,
        end=T0 - timedelta(minutes=30),
    )
    assert (
        constraints("appointments_appointment", "c")["appointment_end_after_start"]
        == 'CHECK (("end" > start))'
    )
    violates("appointment_duration_5_480", AppointmentFactory.create, duration_min=4)
    violates("appointment_duration_5_480", AppointmentFactory.create, duration_min=481)
    violates("appointment_price_non_negative", AppointmentFactory.create, price=-1)
    assert AppointmentFactory.create(price=0, duration_min=5).price == 0
    assert AppointmentFactory.create(duration_min=480).end == T0 + timedelta(minutes=480)


def test_appointment_duration_matches_is_a_raw_check() -> None:
    violates("appointment_duration_matches", AppointmentFactory.create, end=T0 + timedelta(minutes=45))
    violates(
        "appointment_duration_matches",
        AppointmentFactory.create,
        duration_min=20,
        end=T0 + timedelta(minutes=30),
    )
    row = AppointmentFactory.create(duration_min=75)
    assert row.end - row.start == timedelta(minutes=75)
    assert constraints("appointments_appointment", "c")["appointment_duration_matches"] == (
        'CHECK ((("end" - start) = make_interval(mins => (duration_min)::integer)))'
    )


def test_appointment_created_by_matches_requested_via() -> None:
    staff = StaffUserFactory.create()
    violates("appointment_created_by_via", AppointmentFactory.create, requested_via="admin")
    violates("appointment_created_by_via", AppointmentFactory.create, requested_via="app", created_by=staff)
    by_admin = AppointmentFactory.create(requested_via="admin", created_by=staff)
    assert by_admin.created_by == staff
    assert list(staff.created_appointments.all()) == [by_admin]
    with pytest.raises(ProtectedError):
        staff.delete()


def test_appointment_status_fields_check() -> None:
    violates("appointment_status_fields", AppointmentFactory.create, status="requested", confirmed_at=NOW)
    violates("appointment_status_fields", AppointmentFactory.create, status="requested", cancelled_by="admin")
    violates("appointment_status_fields", AppointmentFactory.create, status="confirmed")
    violates(
        "appointment_status_fields",
        AppointmentFactory.create,
        status="confirmed",
        confirmed_at=NOW,
        attended_at=NOW,
    )
    violates("appointment_status_fields", AppointmentFactory.create, status="rejected")
    violates(
        "appointment_status_fields",
        AppointmentFactory.create,
        status="rejected",
        rejected_at=NOW,
        confirmed_at=NOW,
    )
    violates("appointment_status_fields", AppointmentFactory.create, status="attended", attended_at=NOW)
    violates("appointment_status_fields", AppointmentFactory.create, status="cancelled", cancelled_at=NOW)
    violates(
        "appointment_status_fields",
        AppointmentFactory.create,
        status="cancelled",
        cancelled_at=NOW,
        cancelled_by="admin",
        attended_at=NOW,
    )
    AppointmentFactory.create(status="confirmed", confirmed_at=NOW)
    AppointmentFactory.create(status="rejected", rejected_at=NOW)
    AppointmentFactory.create(status="attended", confirmed_at=NOW, attended_at=NOW)
    AppointmentFactory.create(status="cancelled", cancelled_at=NOW, cancelled_by="customer")
    AppointmentFactory.create(status="cancelled", confirmed_at=NOW, cancelled_at=NOW, cancelled_by="admin")
    assert Appointment.objects.count() == 5


def test_appointment_no_overlap_exclusion_per_professional() -> None:
    professional = ProfessionalFactory.create()
    AppointmentFactory.create(professional=professional, start=T0, duration_min=30)
    # same professional, overlapping open slots: requested and confirmed alike
    violates(
        "appointment_no_overlap",
        AppointmentFactory.create,
        professional=professional,
        start=T0 + timedelta(minutes=15),
        duration_min=30,
    )
    violates(
        "appointment_no_overlap",
        AppointmentFactory.create,
        professional=professional,
        start=T0 - timedelta(minutes=10),
        duration_min=20,
        status="confirmed",
        confirmed_at=NOW,
    )
    # '[)' bounds: an appointment that starts exactly at the other's end is fine; another professional too
    AppointmentFactory.create(professional=professional, start=T0 + timedelta(minutes=30), duration_min=30)
    AppointmentFactory.create(start=T0, duration_min=30)
    # closed statuses never block the slot
    other = ProfessionalFactory.create()
    AppointmentFactory.create(professional=other, start=T0, status="rejected", rejected_at=NOW)
    AppointmentFactory.create(
        professional=other, start=T0, status="cancelled", cancelled_at=NOW, cancelled_by="admin"
    )
    AppointmentFactory.create(
        professional=other, start=T0, status="attended", confirmed_at=NOW, attended_at=NOW
    )
    AppointmentFactory.create(professional=other, start=T0)
    assert Appointment.objects.filter(professional=other).count() == 4


def test_appointment_exclusion_is_rechecked_on_update() -> None:
    professional = ProfessionalFactory.create()
    first = AppointmentFactory.create(professional=professional, start=T0)
    second = AppointmentFactory.create(professional=professional, start=T0 + timedelta(hours=1))
    second.start = T0 + timedelta(minutes=10)
    second.end = second.start + timedelta(minutes=30)
    with pytest.raises(IntegrityError, match="appointment_no_overlap"), transaction.atomic():
        second.save()
    first.status = enums.AppointmentStatus.REJECTED
    first.rejected_at = NOW
    first.save()
    second.save()  # the slot was freed
    assert Appointment.objects.get(pk=second.pk).start == T0 + timedelta(minutes=10)


def test_appointment_related_rows_are_protected() -> None:
    row = AppointmentFactory.create()
    for related in (row.professional, row.service, row.customer):
        with pytest.raises(ProtectedError):
            related.delete()
    assert list(row.customer.appointments.all()) == [row]
    assert list(row.professional.appointments.all()) == [row]
    assert list(row.service.appointments.all()) == [row]


def test_appointment_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("appointments_appointment")
    assert indexes["appointment_prof_start_idx"].endswith("USING btree (professional_id, start)")
    assert indexes["appointment_start_idx"].endswith("USING btree (start)")
    assert indexes["appointment_req_start_idx"].endswith(
        "USING btree (start) WHERE ((status)::text = 'requested'::text)"
    )
    assert "USING btree (customer_id, start) WHERE" in indexes["appointment_customer_open_idx"]
    assert "'confirmed'" in indexes["appointment_customer_open_idx"]
    exclusion = constraints("appointments_appointment", "x")["appointment_no_overlap"]
    assert exclusion.startswith(
        "EXCLUDE USING gist (tstzrange(start, \"end\", '[)'::text) WITH &&, professional_id WITH =)"
    )
    assert "WHERE" in exclusion and "'requested'" in exclusion and "'confirmed'" in exclusion
    checks = constraints("appointments_appointment", "c")
    assert set(checks) >= {
        "appointment_end_after_start",
        "appointment_duration_5_480",
        "appointment_price_non_negative",
        "appointment_created_by_via",
        "appointment_status_fields",
        "appointment_duration_matches",
    }


def test_pending_summary_query_uses_the_partial_index_shape() -> None:
    """The shape of the pending summary's query: requested rows from a start instant on, count and earliest
    start (the selector passes the start of today, contract §2.9)."""
    AppointmentFactory.create(start=T0)
    AppointmentFactory.create(start=T0 + timedelta(hours=1), status="confirmed", confirmed_at=NOW)
    AppointmentFactory.create(start=T0 - timedelta(days=5))  # requested but in the past
    pending = Appointment.objects.filter(status="requested", start__gte=NOW)
    assert pending.count() == 1 and pending.order_by("start").first().start == T0  # type: ignore[union-attr]


# --- AppointmentEvent ---------------------------------------------------------------------------------------


def test_event_defaults_str_and_ordering() -> None:
    appointment = AppointmentFactory.create()
    older = AppointmentEventFactory.create(appointment=appointment, at=NOW - timedelta(minutes=5))
    newer = AppointmentEventFactory.create(appointment=appointment, at=NOW)
    same = AppointmentEventFactory.create(appointment=appointment, at=NOW)
    assert list(AppointmentEvent.objects.all()) == [same, newer, older]  # -at, -id
    assert list(appointment.events.all()) == [same, newer, older]
    assert newer.actor is None and newer.data == {"via": "app", "price": 150_000}
    assert str(newer) == f"Appointment {appointment.pk}: requested (customer) at 2026-10-24 12:05"
    assert (
        AppointmentEvent.objects.create(appointment=appointment, type="cancelled", source="system").data == {}
    )


def test_event_actor_by_source_and_source_by_type() -> None:
    staff = StaffUserFactory.create()
    appointment = AppointmentFactory.create()
    create = AppointmentEventFactory.create
    violates("apptevent_actor_by_source", create, appointment=appointment, source="admin", type="requested")
    violates("apptevent_actor_by_source", create, appointment=appointment, source="customer", actor=staff)
    violates("apptevent_actor_by_source", create, appointment=appointment, source="system", actor=staff)
    for type_ in ("confirmed", "rejected", "attended", "rescheduled"):
        violates("apptevent_source_by_type", create, appointment=appointment, source="system", type=type_)
        violates("apptevent_source_by_type", create, appointment=appointment, source="customer", type=type_)
        create(appointment=appointment, source="admin", actor=staff, type=type_)
    create(appointment=appointment, source="admin", actor=staff, type="requested")
    create(appointment=appointment, source="customer", type="cancelled")
    create(appointment=appointment, source="system", type="cancelled")
    assert AppointmentEvent.objects.count() == 7
    assert list(staff.appointment_events.order_by("id").values_list("type", flat=True)) == [
        "confirmed",
        "rejected",
        "attended",
        "rescheduled",
        "requested",
    ]
    with pytest.raises(ProtectedError):
        staff.delete()


def test_event_blocks_appointment_deletion() -> None:
    event = AppointmentEventFactory.create()
    with pytest.raises(ProtectedError):
        event.appointment.delete()
    assert Appointment.objects.filter(pk=event.appointment_id).exists()


def test_event_orm_mutations_are_refused() -> None:
    event = AppointmentEventFactory.create()
    event.type = "confirmed"
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()
    with pytest.raises(AppendOnlyError):
        AppointmentEvent.objects.filter(pk=event.pk).update(type="confirmed")
    with pytest.raises(AppendOnlyError):
        AppointmentEvent.objects.filter(pk=event.pk).delete()
    with pytest.raises(AppendOnlyError):
        AppointmentEvent.objects.bulk_update([event], ["type"])
    event.refresh_from_db()
    assert event.type == "requested"


def test_event_raw_sql_mutations_are_refused_by_the_trigger() -> None:
    event = AppointmentEventFactory.create()
    for statement in (
        "UPDATE appointments_appointmentevent SET type = 'confirmed' WHERE id = %s",
        "DELETE FROM appointments_appointmentevent WHERE id = %s",
    ):
        with (
            pytest.raises(DatabaseError, match="append-only table appointments_appointmentevent"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, [event.pk])
    assert AppointmentEvent.objects.get(pk=event.pk).type == "requested"


def test_event_purge_window_is_refused() -> None:
    """Appointment events are not in PURGEABLE_TABLES: the GUC does not open them (design 03 §0.2)."""
    event = AppointmentEventFactory.create()
    with pytest.raises(DatabaseError, match="append-only"), purge_window(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM appointments_appointmentevent WHERE id = %s", [event.pk])
    assert AppointmentEvent.objects.filter(pk=event.pk).exists()


def test_event_trigger_index_and_checks_in_pg_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'appointments_appointmentevent'::regclass"
            " AND NOT tgisinternal"
        )
        assert [row[0] for row in cursor.fetchall()] == ["appointments_appointmentevent_append_only"]
    indexes = index_definitions("appointments_appointmentevent")
    assert indexes["apptevent_appointment_at_idx"].endswith("USING btree (appointment_id, at, id)")
    assert set(constraints("appointments_appointmentevent", "c")) >= {
        "apptevent_actor_by_source",
        "apptevent_source_by_type",
    }


def test_customer_factory_rows_are_independent() -> None:
    customer = CustomerFactory.create()
    row = AppointmentFactory.create(customer=customer)
    assert row.customer == customer and Appointment.objects.filter(customer=customer).count() == 1
