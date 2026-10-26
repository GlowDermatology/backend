"""loyalty.0001_initial, 0002_append_only and 0003 (design 02 §0.2, 03 §9 row 12): dependencies, reversal."""

from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

LOYALTY_TABLES = {
    "loyalty_cashbackpolicy",
    "loyalty_cashbacktier",
    "loyalty_loyaltyaccount",
    "loyalty_ledgerentry",
    "loyalty_cashbackhold",
    "loyalty_purchase",
    "loyalty_purchaseitem",
    "loyalty_posdevice",
}
TRIGGERS = {
    "loyalty_cashbackpolicy_append_only",
    "loyalty_cashbacktier_append_only",
    "loyalty_ledgerentry_append_only",
}


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def trigger_names() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'loyalty_%%_append_only'")
        return {row[0] for row in cursor.fetchall()}


def test_migration_dependencies_as_designed() -> None:
    initial = import_module("glowderma.loyalty.migrations.0001_initial").Migration
    assert initial.initial is True
    assert initial.dependencies == [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("catalog", "0001_initial"),
        ("commerce", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]
    trigger = import_module("glowderma.loyalty.migrations.0002_append_only").Migration
    assert trigger.dependencies == [
        ("loyalty", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    assert len(trigger.operations) == 3
    for operation, table in zip(trigger.operations, sorted(TRIGGERS), strict=True):
        assert f"CREATE TRIGGER {table} BEFORE UPDATE OR DELETE" in operation.sql
        assert (
            operation.reverse_sql
            == f"DROP TRIGGER IF EXISTS {table} ON {table.removesuffix('_append_only')};"
        )


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    # Another app's transactional test may have left its dependents unapplied; start from a fully migrated
    # database and leave one behind, so the order of transactional tests does not matter.
    call_command("migrate", verbosity=0)
    assert LOYALTY_TABLES <= table_names() and trigger_names() == TRIGGERS
    call_command("migrate", "loyalty", "0001", verbosity=0)
    assert trigger_names() == set() and LOYALTY_TABLES <= table_names()
    call_command("migrate", "loyalty", "zero", verbosity=0)
    assert not (LOYALTY_TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="loyalty").exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_proc WHERE proname = 'glowderma_forbid_mutation'")
        assert cursor.fetchone() == (1,)  # the shared function belongs to core and survives
        cursor.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'commerce_orderevent_append_only'")
        assert cursor.fetchone() == (1,)  # the reverse drops only loyalty's own triggers
    assert "commerce_order" in table_names()  # a dependency is untouched by ``migrate loyalty zero``
    call_command("migrate", "loyalty", verbosity=0)
    assert LOYALTY_TABLES <= table_names() and trigger_names() == TRIGGERS
    assert set(MigrationRecorder.Migration.objects.filter(app="loyalty").values_list("name", flat=True)) == {
        "0001_initial",
        "0002_append_only",
        "0003_posdevice_key_prefix_unique",
        "0004_purchase_billz_payment_attempted_at",
        "0005_purchase_unpushed_online_idx",
    }
    call_command("migrate", verbosity=0)  # re-apply whatever depends on loyalty for the rest of the phase


@pytest.mark.django_db(transaction=True)
def test_the_unique_prefix_step_names_devices_that_share_a_prefix() -> None:
    call_command("migrate", verbosity=0)
    call_command("migrate", "loyalty", "0002", verbosity=0)
    try:
        with connection.cursor() as cursor:
            for name, digest in (("Kassa A", "a" * 64), ("Kassa B", "b" * 64)):
                cursor.execute(
                    "INSERT INTO loyalty_posdevice (name, location_label, key_prefix, key_hash, is_active,"
                    " created_at, updated_at) VALUES (%s, '', 'twin0001', %s, true, now(), now())",
                    [name, digest],
                )
        with pytest.raises(RuntimeError, match="share key prefixes"):
            call_command("migrate", "loyalty", "0003", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM loyalty_posdevice WHERE key_hash = %s", ["b" * 64])
        call_command("migrate", "loyalty", "0003", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM loyalty_posdevice")
    finally:
        call_command("migrate", verbosity=0)
