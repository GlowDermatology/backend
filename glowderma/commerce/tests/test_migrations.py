"""commerce.0001–0003 (design 03 §9 rows 9–11, §10): dependencies, the order sequence and reversibility."""

from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from glowderma.commerce.models import Order
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core.seeding import reset_sequences

COMMERCE_TABLES = {
    "commerce_cart",
    "commerce_cartitem",
    "commerce_order",
    "commerce_orderitem",
    "commerce_orderevent",
    "commerce_stockreservation",
}


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def sequence_start_value() -> int | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT start_value FROM pg_sequences WHERE schemaname = current_schema()"
            " AND sequencename = 'commerce_order_id_seq'"
        )
        row = cursor.fetchone()
    return int(row[0]) if row else None


def next_order_id() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT last_value, is_called FROM commerce_order_id_seq")
        last_value, is_called = cursor.fetchone()
    return int(last_value) + 1 if is_called else int(last_value)


def trigger_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'commerce_orderevent_append_only'")
        return cursor.fetchone() is not None


def test_migration_dependencies_as_designed() -> None:
    initial = import_module("glowderma.commerce.migrations.0001_initial").Migration
    assert initial.initial is True
    assert initial.dependencies == [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("catalog", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]
    sequence = import_module("glowderma.commerce.migrations.0002_order_sequence").Migration
    assert sequence.dependencies == [("commerce", "0001_initial")]
    (operation,) = sequence.operations
    assert "START WITH 10001 RESTART WITH 10001" in operation.sql
    assert "START WITH 1 RESTART WITH 1" in operation.reverse_sql
    trigger = import_module("glowderma.commerce.migrations.0003_orderevent_append_only").Migration
    assert trigger.dependencies == [
        ("commerce", "0002_order_sequence"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]


@pytest.mark.django_db
def test_order_sequence_starts_at_10001() -> None:
    assert sequence_start_value() == 10001
    assert reset_sequences() > 0  # an empty table goes back to the configured start
    assert next_order_id() == 10001
    first = OrderFactory.create()
    assert first.pk == 10001
    second = OrderFactory.create()
    assert second.pk == 10002
    # with rows present the next value is MAX(id) + 1 (03 §7.9: GREATEST(MAX(id), floor))
    reset_sequences()
    assert next_order_id() == 10003
    assert OrderFactory.create().pk == 10003


@pytest.mark.django_db
def test_reset_sequences_keeps_10001_on_an_empty_order_table() -> None:
    assert not Order.objects.exists()
    reset_sequences()
    assert next_order_id() == 10001
    reset_sequences()  # idempotent: setval(…, 10001, false) again, never 10002
    assert next_order_id() == 10001


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    # Another app's transactional test may have left its dependents (this app) unapplied; start from a fully
    # migrated database and leave one behind, so the order of transactional tests does not matter.
    call_command("migrate", verbosity=0)
    assert COMMERCE_TABLES <= table_names() and trigger_exists()
    call_command("migrate", "commerce", "0002", verbosity=0)
    assert not trigger_exists()
    call_command("migrate", "commerce", "0001", verbosity=0)
    assert sequence_start_value() == 1 and next_order_id() == 1  # 0002 reversed
    call_command("migrate", "commerce", "zero", verbosity=0)
    assert not (COMMERCE_TABLES & table_names())
    assert sequence_start_value() is None
    assert not MigrationRecorder.Migration.objects.filter(app="commerce").exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_proc WHERE proname = 'glowderma_forbid_mutation'")
        assert cursor.fetchone() == (1,)  # the shared function belongs to core and survives
    call_command("migrate", "commerce", verbosity=0)
    assert COMMERCE_TABLES <= table_names() and trigger_exists()
    assert sequence_start_value() == 10001 and next_order_id() == 10001
    assert {row.name for row in MigrationRecorder.Migration.objects.filter(app="commerce")} == {
        "0001_initial",
        "0002_order_sequence",
        "0003_orderevent_append_only",
        "0004_cartitem_price_at_add",
        "0005_orderitem_image_path",
        "0006_stockreservation_billz_settled_at",
        "0007_settle_billz_reservations",
        "0008_backfill_refund_manual_reason",
    }
    assert OrderFactory.create().pk == 10001
    call_command("migrate", verbosity=0)  # re-apply any app that depends on commerce (loyalty, payments, …)
