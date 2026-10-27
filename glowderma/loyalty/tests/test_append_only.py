"""``CashbackPolicy``, ``CashbackTier`` and ``LedgerEntry`` are append-only (design 02 §0.2): ORM, raw SQL and
the purge GUC are all refused, on every one of the three tables."""

from collections.abc import Callable

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.models import Model

from glowderma.core.db import PURGEABLE_TABLES, purge_window
from glowderma.core.models import AppendOnlyError
from glowderma.loyalty.models import CashbackPolicy, CashbackTier, LedgerEntry
from glowderma.loyalty.tests.factories import CashbackPolicyFactory, CashbackTierFactory, LedgerEntryFactory

pytestmark = pytest.mark.django_db

Case = tuple[type[Model], Callable[[], Model], str, str]
CASES: list[Case] = [
    (CashbackPolicy, lambda: CashbackPolicyFactory.create(tiers=[]), "basis", "with_purchase"),
    (CashbackTier, lambda: CashbackTierFactory.create(), "from_amount", "1"),
    (LedgerEntry, lambda: LedgerEntryFactory.create(), "source_ref", "tampered"),
]
TABLES = ["loyalty_cashbackpolicy", "loyalty_cashbacktier", "loyalty_ledgerentry"]


@pytest.mark.parametrize(("model", "make", "column", "value"), CASES, ids=[c[0].__name__ for c in CASES])
def test_orm_mutations_are_refused(
    model: type[Model], make: Callable[[], Model], column: str, value: str
) -> None:
    row = make()
    original = getattr(row, column)
    setattr(row, column, value)
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.save(update_fields=[column])
    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        model._default_manager.filter(pk=row.pk).update(**{column: value})
    with pytest.raises(AppendOnlyError):
        model._default_manager.filter(pk=row.pk).delete()
    with pytest.raises(AppendOnlyError):
        model._default_manager.all().delete()
    with pytest.raises(AppendOnlyError):
        model._default_manager.bulk_update([row], [column])
    row.refresh_from_db()
    assert getattr(row, column) == original


@pytest.mark.parametrize(("model", "make", "column", "value"), CASES, ids=[c[0].__name__ for c in CASES])
def test_raw_sql_mutations_are_refused_by_the_trigger(
    model: type[Model], make: Callable[[], Model], column: str, value: str
) -> None:
    row = make()
    table = model._meta.db_table
    with (
        pytest.raises(DatabaseError, match=f"append-only table {table}"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, row.pk])  # noqa: S608
    with (
        pytest.raises(DatabaseError, match=f"append-only table {table}"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row.pk])  # noqa: S608
    assert model._default_manager.filter(pk=row.pk).exists()


@pytest.mark.parametrize(("model", "make", "column", "value"), CASES, ids=[c[0].__name__ for c in CASES])
def test_not_purgeable_even_inside_the_purge_window(
    model: type[Model], make: Callable[[], Model], column: str, value: str
) -> None:
    table = model._meta.db_table
    assert table not in PURGEABLE_TABLES
    row = make()
    with (
        pytest.raises(DatabaseError, match=f"append-only table {table}"),
        purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row.pk])  # noqa: S608
    with (
        pytest.raises(DatabaseError, match=f"append-only table {table}"),
        purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", [value, row.pk])  # noqa: S608
    assert model._default_manager.filter(pk=row.pk).exists()


@pytest.mark.parametrize("table", TABLES)
def test_trigger_in_pg_catalog(table: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname, tgtype FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal", [table]
        )
        rows = cursor.fetchall()
    assert [row[0] for row in rows] == [f"{table}_append_only"]
    # tgtype bits: 1 = ROW, 2 = BEFORE, 8 = DELETE, 16 = UPDATE → BEFORE UPDATE OR DELETE FOR EACH ROW
    assert rows[0][1] & (1 | 2 | 8 | 16) == 1 | 2 | 8 | 16


def test_mutable_tables_carry_no_trigger() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE NOT t.tgisinternal AND c.relname LIKE 'loyalty_%%'"
        )
        assert sorted(row[0] for row in cursor.fetchall()) == TABLES
