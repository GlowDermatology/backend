"""catalog.0001_initial: dependencies, reversibility and the DB objects it creates."""

from importlib import import_module

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_initial_depends_only_on_core_0007_and_accounts_0001() -> None:
    migration = import_module("glowderma.catalog.migrations.0001_initial").Migration
    assert migration.dependencies == [("core", "0007_db_extensions"), ("accounts", "0001_initial")]
    assert migration.initial is True


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    catalog_tables = {
        "catalog_category",
        "catalog_product",
        "catalog_productstock",
        "catalog_productimage",
        "catalog_banner",
    }
    assert catalog_tables <= table_names()
    call_command("migrate", "catalog", "zero", verbosity=0)
    assert not (catalog_tables & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="catalog").exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        assert cursor.fetchone() == (1,)  # the extension belongs to core and survives
    call_command("migrate", "catalog", verbosity=0)
    assert catalog_tables <= table_names()
    assert MigrationRecorder.Migration.objects.filter(app="catalog", name="0001_initial").exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'catalog_product_search_trgm'")
        assert "gin_trgm_ops" in cursor.fetchone()[0]
    # ``migrate catalog zero`` also unapplied every app depending on catalog (notifications, ...); the forward
    # ``migrate catalog`` did not bring them back, so leave the test DB fully migrated for the tests after us
    call_command("migrate", verbosity=0)
    assert "notifications_campaign" in table_names()
