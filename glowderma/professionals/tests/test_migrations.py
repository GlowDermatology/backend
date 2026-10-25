"""professionals.0001_initial: dependencies, reversibility and the constraints it creates."""

from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

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
