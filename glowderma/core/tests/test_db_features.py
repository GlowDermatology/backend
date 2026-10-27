"""core.0007 / core.0008: the global DB objects and the retention purge window (decision T6)."""

import pytest
from django.db import DatabaseError, connection, transaction

from glowderma.core.db import PURGEABLE_TABLES, purge_window
from glowderma.core.models import AuditEntry


def make_entry() -> AuditEntry:
    return AuditEntry.objects.create(
        actor_type="system", action="test.purge", entity_type="test", entity_id="1"
    )


@pytest.mark.django_db
def test_extensions_and_collation_exist() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm', 'btree_gist') ORDER BY 1"
        )
        assert [row[0] for row in cursor.fetchall()] == ["btree_gist", "pg_trgm"]
        cursor.execute(
            "SELECT collprovider, collisdeterministic FROM pg_collation WHERE collname = 'glowderma_ci'"
        )
        assert cursor.fetchone() == ("i", False)


@pytest.mark.django_db
def test_collation_orders_case_insensitively_and_keeps_accents_apart() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT v FROM (VALUES ('b'), ('A'), ('Ёлка'), ('a'), ('B'), ('ёлка')) AS t(v) "
            "ORDER BY v COLLATE glowderma_ci, v"
        )
        values = [row[0].lower() for row in cursor.fetchall()]
    assert values == ["a", "a", "b", "b", "ёлка", "ёлка"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT 'ánd' COLLATE glowderma_ci = 'and', 'ZINK' COLLATE glowderma_ci = 'zink'")
        assert cursor.fetchone() == (False, True)


@pytest.mark.django_db
def test_raw_delete_is_refused_without_the_purge_window() -> None:
    entry = make_entry()
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM core_auditentry WHERE id = %s", [entry.pk])


@pytest.mark.django_db
def test_purge_window_allows_delete_on_a_purgeable_table() -> None:
    entry = make_entry()
    with purge_window(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM core_auditentry WHERE id = %s", [entry.pk])
    assert not AuditEntry.objects.filter(pk=entry.pk).exists()


@pytest.mark.django_db
def test_purge_window_never_allows_update() -> None:
    entry = make_entry()
    with pytest.raises(DatabaseError, match="append-only"), purge_window(), connection.cursor() as cursor:
        cursor.execute("UPDATE core_auditentry SET reason = 'x' WHERE id = %s", [entry.pk])


@pytest.mark.django_db(transaction=True)
def test_purge_window_is_transaction_local() -> None:
    """SET LOCAL ends with the transaction: the next statement is append-only again."""
    entry = make_entry()
    with purge_window():
        pass
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM core_auditentry WHERE id = %s", [entry.pk])
    assert AuditEntry.objects.filter(pk=entry.pk).exists()


def test_purgeable_tables_are_the_two_decided_ones() -> None:
    assert PURGEABLE_TABLES == ("core_auditentry", "integrations_webhookinbox")
