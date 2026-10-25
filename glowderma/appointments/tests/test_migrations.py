"""appointments.0001_initial / 0002_appointmentevent_append_only: dependencies, reversibility, DB objects."""

from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

TABLES = {"appointments_appointment", "appointments_appointmentevent"}


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def constraint_names(table: str, kind: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = %s", [table, kind]
        )
        return {row[0] for row in cursor.fetchall()}


def trigger_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'appointments_appointmentevent_append_only'")
        return cursor.fetchone() is not None


def test_dependencies_follow_the_design() -> None:
    initial = import_module("glowderma.appointments.migrations.0001_initial").Migration
    assert initial.initial is True
    assert initial.dependencies == [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("professionals", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]
    trigger = import_module("glowderma.appointments.migrations.0002_appointmentevent_append_only").Migration
    assert trigger.dependencies == [
        ("appointments", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]


@pytest.mark.django_db(transaction=True)
def test_btree_gist_exists_and_backs_the_exclusion() -> None:
    call_command("migrate", verbosity=0)  # a sibling's ``migrate <dependency> zero`` may have unapplied us
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
        assert cursor.fetchone() == (1,)
    assert "appointment_no_overlap" in constraint_names("appointments_appointment", "x")
    assert "appointment_duration_matches" in constraint_names("appointments_appointment", "c")
    assert trigger_exists()


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    call_command("migrate", verbosity=0)  # a sibling's ``migrate <dependency> zero`` may have unapplied us
    assert TABLES <= table_names()
    call_command("migrate", "appointments", "0001", verbosity=0)
    assert not trigger_exists()
    call_command("migrate", "appointments", "zero", verbosity=0)
    assert not (TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="appointments").exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
        assert cursor.fetchone() == (1,)  # the extension belongs to core and survives
    call_command("migrate", "appointments", verbosity=0)
    assert TABLES <= table_names()
    assert trigger_exists()
    assert "appointment_no_overlap" in constraint_names("appointments_appointment", "x")
    assert "appointment_duration_matches" in constraint_names("appointments_appointment", "c")
    assert set(
        MigrationRecorder.Migration.objects.filter(app="appointments").values_list("name", flat=True)
    ) == {
        "0001_initial",
        "0002_appointmentevent_append_only",
    }
