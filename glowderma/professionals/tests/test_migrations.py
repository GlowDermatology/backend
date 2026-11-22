"""professionals migrations: dependencies, reversibility, the constraints they create, the service
categories 0002 infers and the per-specialist split of shared services (0010–0012)."""

from datetime import timedelta
from importlib import import_module
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import timeutils
from glowderma.professionals.models import SERVICE_NAME_CONSTRAINT, Location, Service
from glowderma.professionals.services import OVERRIDE_CONSTRAINT, STAFF_USER_CONSTRAINT
from glowderma.professionals.tests.factories import ProfessionalFactory

TABLES = {
    "professionals_location",
    "professionals_professional",
    "professionals_service",
    "professionals_workinghours",
    "professionals_workinghoursoverride",
    "professionals_blockedperiod",
}
CHECKS = {
    "professionals_location": ["location_name_uz_required", "location_phone_e164", "location_latlng_pair"],
    "professionals_professional": ["professional_name_required", "professional_slot_step_allowed"],
    "professionals_service": [
        "service_names_uz_required",
        "service_duration_5_480",
        "service_price_non_negative",
    ],
    "professionals_workinghours": [
        "workinghours_weekday_0_6",
        "workinghours_start_before_end",
        "workinghours_break_inside_day",
    ],
    "professionals_workinghoursoverride": ["hoursoverride_both_or_none"],
    "professionals_blockedperiod": ["blockedperiod_end_after_start"],
}
UNIQUES = {
    "professionals_service": SERVICE_NAME_CONSTRAINT,
    "professionals_workinghours": "workinghours_professional_weekday_unique",
    "professionals_workinghoursoverride": "hoursoverride_professional_date_unique",
}


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def constraint_names(table: str, kind: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = %s", [table, kind]
        )
        return {row[0] for row in cursor.fetchall()}


def test_initial_depends_only_on_accounts_0001() -> None:
    migration = import_module("glowderma.professionals.migrations.0001_initial").Migration
    assert migration.dependencies == [("accounts", "0001_initial")]
    assert migration.initial is True


@pytest.mark.django_db
def test_constraints_and_indexes_exist() -> None:
    for table, names in CHECKS.items():
        assert set(names) <= constraint_names(table, "c"), table
    for table, name in UNIQUES.items():
        assert name in constraint_names(table, "u"), table
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename LIKE 'professionals_%%'")
        definitions = dict(cursor.fetchall())
    assert '(is_active, "position", name)' in definitions["prof_professional_active_pos"]
    assert "(professional_id, start, " in definitions["prof_blocked_prof_range"]
    assert '(professional_id, is_active, "position")' in definitions["prof_service_owner_pos"]
    assert "professionals_professionalservice" not in table_names()


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    assert TABLES <= table_names()
    call_command("migrate", "professionals", "zero", verbosity=0)
    assert not (TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="professionals").exists()
    call_command("migrate", "professionals", verbosity=0)
    assert TABLES <= table_names()
    assert MigrationRecorder.Migration.objects.filter(app="professionals", name="0001_initial").exists()
    assert "workinghours_break_inside_day" in constraint_names("professionals_workinghours", "c")
    assert MigrationRecorder.Migration.objects.filter(
        app="professionals", name="0002_qualifications_staff_user_service_category"
    ).exists()
    assert {"qualifications_uz", "staff_user_id"} <= column_names("professionals_professional")
    assert "category" in column_names("professionals_service")
    # ``migrate professionals zero`` also unapplied every app depending on professionals (appointments, ...);
    # the forward ``migrate professionals`` did not bring them back, so leave the test DB fully migrated
    call_command("migrate", verbosity=0)
    assert "appointments_appointment" in table_names()


def column_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


def test_0002_depends_on_0001_and_accounts() -> None:
    migration = import_module(
        "glowderma.professionals.migrations.0002_qualifications_staff_user_service_category"
    ).Migration
    assert migration.dependencies == [("professionals", "0001_initial"), ("accounts", "0001_initial")]


@pytest.mark.django_db(transaction=True)
def test_0011_splits_shared_services_per_specialist_and_repoints_their_appointments() -> None:
    """Back to 0010 (the link model again), the shapes of the previous release, then forward: one service row
    per specialist, the booked rows keep their ids, appointments follow their specialist's copy, an unowned
    service is deleted, a repeated name of one specialist is made unique, and a full ``migrate`` restores
    the rest."""
    call_command("migrate", verbosity=0)  # a sibling's ``migrate <dependency> zero`` may have unapplied us
    before, after = ("professionals", "0010_service_owner"), ("professionals", "0011_split_shared_services")
    targets = [before, ("appointments", "0006_appointment_clinic")]  # the state must hold Appointment too
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    old = executor.loader.project_state(targets).apps
    location = old.get_model("professionals", "Location").objects.create(name_uz="Klinika")
    user_model = old.get_model("accounts", "StaffUser")
    professional_model = old.get_model("professionals", "Professional")

    def specialist(name: str) -> Any:
        account = user_model.objects.create(email=f"{name.lower()}@x.uz", full_name=name, role="specialist")
        return professional_model.objects.create(
            location=location, staff_user=account, name=name, slug=name.lower()
        )

    nodira, jasur, dilnoza = specialist("Nodira"), specialist("Jasur"), specialist("Dilnoza")
    service_model = old.get_model("professionals", "Service")
    link_model = old.get_model("professionals", "ProfessionalService")

    def service(name: str, **fields: Any) -> Any:
        return service_model.objects.create(name_uz=name, short_name_uz=name[:40], duration_min=30, **fields)

    shared = service("Konsultatsiya", position=4, description_ru="Описание")
    unowned = service("Hech kim")
    orphan_booked = service("Eski xizmat", is_active=False)
    same_name = service("Konsultatsiya")  # Nodira's second "Konsultatsiya"
    link_model.objects.create(professional=nodira, service=shared, price=150_000, duration_min=30)
    link_model.objects.create(
        professional=jasur, service=shared, price=120_000, duration_min=40, is_active=False
    )
    link_model.objects.create(professional=nodira, service=same_name, price=1, duration_min=30)
    customer = old.get_model("accounts", "Customer").objects.create(phone="+998901234567", name="Malika")
    appointment_model = old.get_model("appointments", "Appointment")
    clock = timeutils.now().replace(microsecond=0)

    def book(professional: Any, booked: Any, days: int, price: int) -> Any:
        start = clock + timedelta(days=days)
        return appointment_model.objects.create(
            professional=professional,
            location=location,
            service=booked,
            customer=customer,
            start=start,
            end=start + timedelta(minutes=30),
            duration_min=30,
            price=price,
            status="attended",
            requested_via="app",
            requested_at=start - timedelta(days=1),
            confirmed_at=start - timedelta(hours=12),
            attended_at=start,
        )

    by_nodira = book(nodira, shared, 1, 150_000)
    by_jasur = book(jasur, shared, 2, 120_000)
    by_dilnoza = book(dilnoza, orphan_booked, 3, 90_000)  # booked without a link: Dilnoza owns it now

    targets = [after, ("appointments", "0006_appointment_clinic")]
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    new = executor.loader.project_state(targets).apps
    rows = new.get_model("professionals", "Service").objects
    first = rows.get(pk=shared.pk)
    assert (first.professional_id, first.price, first.duration_min, first.is_active) == (
        nodira.pk,
        150_000,
        30,
        True,
    )
    copy = rows.get(professional_id=jasur.pk)
    assert copy.pk != shared.pk
    assert (copy.name_uz, copy.position, copy.description_ru) == ("Konsultatsiya", 4, "Описание")
    assert (copy.price, copy.duration_min, copy.is_active) == (120_000, 40, False)  # the paused link
    kept = rows.get(pk=orphan_booked.pk)
    assert (kept.professional_id, kept.price, kept.is_active) == (dilnoza.pk, 90_000, False)
    assert not rows.filter(pk=unowned.pk).exists()
    assert rows.get(pk=same_name.pk).name_uz == f"Konsultatsiya #{same_name.pk}"
    appointments = new.get_model("appointments", "Appointment").objects
    assert appointments.get(pk=by_nodira.pk).service_id == shared.pk
    assert appointments.get(pk=by_jasur.pk).service_id == copy.pk
    assert appointments.get(pk=by_dilnoza.pk).service_id == orphan_booked.pk

    call_command("migrate", verbosity=0)  # 0012: owner and price required, the link model gone
    assert "professionals_professionalservice" not in table_names()
    assert Service.objects.filter(professional__isnull=True).count() == 0
    call_command("migrate", "professionals", "0010", verbosity=0)  # the reverse links every row again
    assert "professionals_professionalservice" in table_names()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM professionals_professionalservice")
        assert cursor.fetchone() == (4,)
    call_command("migrate", verbosity=0)


@pytest.mark.django_db
def test_the_previous_release_can_still_insert_rows() -> None:
    """The new NOT NULL columns have database defaults: an INSERT without them (a process of the previous
    release during the rollout) succeeds."""
    location = Location.objects.create(name_uz="Klinika")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO professionals_service (created_at, updated_at, name_uz, name_ru, name_en, "
            "short_name_uz, short_name_ru, short_name_en, description_uz, description_ru, description_en, "
            "duration_min, price, position, is_active, professional_id) VALUES (now(), now(), 'Eski', '', "
            "'', 'Eski', '', '', '', '', '', 30, 0, 0, true, %s) RETURNING category",
            [ProfessionalFactory.create().pk],
        )
        assert cursor.fetchone() == ("dermatology",)
        account = StaffUserFactory.create(role="specialist")
        cursor.execute(
            "INSERT INTO professionals_professional (created_at, updated_at, location_id, staff_user_id, "
            "name, role, photo, bio_uz, bio_ru, bio_en, slot_step_min, position, is_active) VALUES (now(), "
            "now(), %s, %s, 'Eski', 'dermatologist', '', '', '', '', 30, 0, true) RETURNING "
            "qualifications_uz, slug",
            [location.pk, account.pk],
        )
        assert cursor.fetchone() == ("", "")


@pytest.mark.django_db
def test_the_unique_names_the_services_translate_exist() -> None:
    """``services._save_unique`` maps a lost race on these constraints to a 400 ``unique``: the names must be
    PostgreSQL's (the one-to-one gets the default ``<table>_<column>_key``)."""
    assert STAFF_USER_CONSTRAINT in constraint_names("professionals_professional", "u")
    assert OVERRIDE_CONSTRAINT in constraint_names("professionals_workinghoursoverride", "u")
