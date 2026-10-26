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


def search_text_expression() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = 'catalog_product'::regclass AND a.attname = 'search_text'"
        )
        return str(cursor.fetchone()[0])


def search_index() -> str | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'catalog_product_search_trgm'")
        row = cursor.fetchone()
    return None if row is None else str(row[0])


@pytest.mark.django_db(transaction=True)
def test_search_fold_migrations_round_trip() -> None:
    """0002 rebuilds the generated column folded (GC-1), 0003 its trigram index concurrently; both reverse."""
    from glowderma.catalog.tests.factories import ProductFactory

    product = ProductFactory.create(name_uz="Yumshoq tozalovchi ko‘pik", billz_id="4607099901")
    assert search_text_expression().startswith("translate(lower(")
    assert "gin_trgm_ops" in (search_index() or "")
    try:
        call_command("migrate", "catalog", "0001", verbosity=0)
        assert search_text_expression().lower().startswith("lower(")
        assert "gin_trgm_ops" in (search_index() or "")
        with connection.cursor() as cursor:
            cursor.execute("SELECT search_text FROM catalog_product WHERE id = %s", [product.pk])
            assert "ko‘pik" in cursor.fetchone()[0]
        call_command("migrate", "catalog", "0002", verbosity=0)
        assert search_index() is None  # the old index went with the old column; 0003 builds the new one
    finally:
        call_command("migrate", verbosity=0)
    assert search_text_expression().startswith("translate(lower(")
    assert "gin_trgm_ops" in (search_index() or "")
    with connection.cursor() as cursor:
        cursor.execute("SELECT search_text FROM catalog_product WHERE id = %s", [product.pk])
        assert "ko'pik" in cursor.fetchone()[0]


def test_search_index_migration_is_concurrent() -> None:
    migration = import_module("glowderma.catalog.migrations.0003_product_search_trgm_concurrently").Migration
    assert migration.atomic is False
    assert [type(op).__name__ for op in migration.operations] == ["AddIndexConcurrently"]


@pytest.mark.django_db(transaction=True)
def test_banner_url_migration_reverses_url_links_to_none() -> None:
    """0004 (gap X-BANNER-URL): the reverse turns URL banners into banners without a link first."""
    from glowderma.catalog.models import Banner
    from glowderma.catalog.tests.factories import BannerFactory

    banner = BannerFactory.create(link_kind="url", link_url="https://glowderma.uz/aksiya")
    try:
        call_command("migrate", "catalog", "0003", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("SELECT link_kind FROM catalog_banner WHERE id = %s", [banner.pk])
            assert cursor.fetchone() == ("none",)
    finally:
        call_command("migrate", verbosity=0)
    assert Banner.objects.get(pk=banner.pk).link_url == ""
