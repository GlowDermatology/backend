"""``OrderEvent`` is append-only (design 03 §0.2): ORM, raw SQL and the purge GUC are all refused."""

import pytest
from django.db import DatabaseError, connection, transaction

from glowderma.commerce.models import OrderEvent
from glowderma.commerce.tests.factories import OrderEventFactory
from glowderma.core.db import PURGEABLE_TABLES, purge_window
from glowderma.core.models import AppendOnlyError

pytestmark = pytest.mark.django_db


def test_orderevent_orm_mutations_are_refused() -> None:
    event = OrderEventFactory.create()
    event.data = {"tampered": True}
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()
    with pytest.raises(AppendOnlyError):
        OrderEvent.objects.filter(pk=event.pk).update(data={"tampered": True})
    with pytest.raises(AppendOnlyError):
        OrderEvent.objects.filter(pk=event.pk).delete()
    with pytest.raises(AppendOnlyError):
        OrderEvent.objects.bulk_update([event], ["data"])
    event.refresh_from_db()
    assert event.data == {"channel": "ios", "item_count": 1, "total": 210_000}


def test_orderevent_raw_sql_mutations_are_refused_by_the_trigger() -> None:
    event = OrderEventFactory.create()
    with (
        pytest.raises(DatabaseError, match="append-only table commerce_orderevent"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE commerce_orderevent SET source = 'bts' WHERE id = %s", [event.pk])
    with (
        pytest.raises(DatabaseError, match="append-only table commerce_orderevent"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM commerce_orderevent WHERE id = %s", [event.pk])
    assert OrderEvent.objects.filter(pk=event.pk).exists()


def test_orderevent_is_not_purgeable_even_inside_the_purge_window() -> None:
    assert "commerce_orderevent" not in PURGEABLE_TABLES
    event = OrderEventFactory.create()
    with (
        pytest.raises(DatabaseError, match="append-only table commerce_orderevent"),
        purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM commerce_orderevent WHERE id = %s", [event.pk])
    with (
        pytest.raises(DatabaseError, match="append-only table commerce_orderevent"),
        purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE commerce_orderevent SET source = 'bts' WHERE id = %s", [event.pk])
    assert OrderEvent.objects.filter(pk=event.pk).exists()


def test_orderevent_trigger_in_pg_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname, tgtype FROM pg_trigger WHERE tgrelid = 'commerce_orderevent'::regclass"
            " AND NOT tgisinternal"
        )
        rows = cursor.fetchall()
    assert [row[0] for row in rows] == ["commerce_orderevent_append_only"]
    # tgtype bits: 1 = ROW, 2 = BEFORE, 8 = DELETE, 16 = UPDATE → BEFORE UPDATE OR DELETE FOR EACH ROW
    assert rows[0][1] & (1 | 2 | 8 | 16) == 1 | 2 | 8 | 16
