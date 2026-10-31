"""delivery.0001–0002 (design 03 §9 row 14, §10): dependencies, the trigger and reversibility."""

import os
from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from glowderma.core.db import append_only_trigger_sql
from glowderma.delivery.tests.factories import ShipmentEventFactory

DELIVERY_TABLES = {"delivery_shipment", "delivery_shipmentevent"}


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def trigger_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'delivery_shipmentevent_append_only'")
        return cursor.fetchone() is not None


def index_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", [table])
        return {row[0] for row in cursor.fetchall()}


def test_migration_dependencies_as_designed() -> None:
    initial = import_module("glowderma.delivery.migrations.0001_initial").Migration
    assert initial.initial is True
    # commerce.0001 only (never 0002/0003) and a core migration at or after 0006 (OutboxMessage.claim_token)
    assert initial.dependencies == [("commerce", "0001_initial"), ("core", "0007_db_extensions")]
    created = {op.name for op in initial.operations if op.__class__.__name__ == "CreateModel"}
    assert created == {"Shipment", "ShipmentEvent"}
    trigger = import_module("glowderma.delivery.migrations.0002_append_only").Migration
    assert trigger.dependencies == [
        ("delivery", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    (operation,) = trigger.operations
    forward, reverse = append_only_trigger_sql("delivery_shipmentevent")
    assert operation.sql == forward and operation.reverse_sql == reverse
    assert "current_setting('glowderma.allow_purge', true)" in forward  # the GUC-aware body of core.0008

    from glowderma.delivery import migrations as package

    names = sorted(name for name in os.listdir(package.__path__[0]) if name[0].isdigit())
    assert names == [
        "0001_initial.py",
        "0002_append_only.py",
        "0003_uz_verbose_names.py",
        "0004_uz_choice_labels.py",
        "0005_uz_field_labels.py",
    ]


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    # Another app's transactional test may have left its dependents unapplied; start from a fully migrated
    # database and leave one behind, so the order of transactional tests does not matter.
    call_command("migrate", verbosity=0)
    assert DELIVERY_TABLES <= table_names() and trigger_exists()
    call_command("migrate", "delivery", "0001", verbosity=0)
    assert not trigger_exists() and DELIVERY_TABLES <= table_names()
    call_command("migrate", "delivery", "zero", verbosity=0)
    assert not (DELIVERY_TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="delivery").exists()
    assert {"commerce_order", "core_outboxmessage"} <= table_names()  # the FK targets are untouched
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_proc WHERE proname = 'glowderma_forbid_mutation'")
        assert cursor.fetchone() == (1,)  # the shared function belongs to core and survives
    call_command("migrate", "delivery", verbosity=0)
    assert DELIVERY_TABLES <= table_names() and trigger_exists()
    assert {"shipment_state_idx", "shipment_tracking_idx"} <= index_names("delivery_shipment")
    assert "shipmentevent_shipment_at_idx" in index_names("delivery_shipmentevent")
    assert {row.name for row in MigrationRecorder.Migration.objects.filter(app="delivery")} == {
        "0001_initial",
        "0002_append_only",
        "0003_uz_verbose_names",
        "0004_uz_choice_labels",
        "0005_uz_field_labels",
    }
    event = ShipmentEventFactory.create()
    assert event.pk is not None
    call_command("migrate", verbosity=0)  # re-apply any app that depends on delivery (none today)
