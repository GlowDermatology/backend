"""professionals.0001_initial / 0002: dependencies, reversibility, the constraints they create and the
service categories 0002 infers."""

from importlib import import_module

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from glowderma.core import enums
from glowderma.professionals.models import Professional, Service
from glowderma.professionals.tests.factories import ProfessionalServiceFactory, ServiceFactory

TABLES = {
    "professionals_location",
    "professionals_professional",
    "professionals_service",
    "professionals_professionalservice",
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
        "service_default_price_non_negative",
    ],
    "professionals_professionalservice": [
        "professionalservice_price_non_negative",
        "professionalservice_duration_5_480",
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
    "professionals_professionalservice": "professionalservice_unique",
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


@pytest.mark.django_db
def test_0002_infers_the_category_from_who_offers_the_service() -> None:
    infer = import_module(
        "glowderma.professionals.migrations.0002_qualifications_staff_user_service_category"
    ).infer_categories
    cosmetic = ProfessionalServiceFactory.create(professional__role="cosmetologist").service
    both = ProfessionalServiceFactory.create(professional__role="cosmetologist").service
    ProfessionalServiceFactory.create(service=both, professional__role="dermatologist")
    dermatologic = ProfessionalServiceFactory.create(professional__role="dermatologist").service
    nobody = ServiceFactory.create()
    infer(apps, None)
    infer(apps, None)  # deterministic: a second run changes nothing
    categories = dict(Service.objects.values_list("pk", "category"))
    assert categories == {
        cosmetic.pk: enums.ServiceCategory.COSMETOLOGY,
        both.pk: enums.ServiceCategory.DERMATOLOGY,
        dermatologic.pk: enums.ServiceCategory.DERMATOLOGY,
        nobody.pk: enums.ServiceCategory.DERMATOLOGY,
    }


@pytest.mark.django_db
def test_the_previous_release_can_still_insert_rows() -> None:
    """The new NOT NULL columns have database defaults: an INSERT without them (a process of the previous
    release during the rollout) succeeds."""
    location = Professional._meta.get_field("location").related_model.objects.create(name_uz="Klinika")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO professionals_service (created_at, updated_at, name_uz, name_ru, name_en, "
            "short_name_uz, short_name_ru, short_name_en, description_uz, description_ru, description_en, "
            "default_duration_min, position, is_active) VALUES (now(), now(), 'Eski', '', '', 'Eski', "
            "'', '', '', '', '', 30, 0, true) RETURNING category"
        )
        assert cursor.fetchone() == ("dermatology",)
        cursor.execute(
            "INSERT INTO professionals_professional (created_at, updated_at, location_id, name, role, photo, "
            "bio_uz, bio_ru, bio_en, slot_step_min, position, is_active) VALUES (now(), now(), %s, 'Eski', "
            "'dermatologist', '', '', '', '', 30, 0, true) RETURNING qualifications_uz, staff_user_id",
            [location.pk],
        )
        assert cursor.fetchone() == ("", None)

