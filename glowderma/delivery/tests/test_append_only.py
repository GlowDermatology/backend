"""``ShipmentEvent`` is append-only (design 02 §0.2): ORM, raw SQL and the purge GUC are all refused."""

import pytest
from django.db import DatabaseError, connection, transaction

from glowderma.core.db import PURGEABLE_TABLES, purge_window
from glowderma.core.models import AppendOnlyError
from glowderma.delivery.models import ShipmentEvent
from glowderma.delivery.tests.factories import ShipmentEventFactory

pytestmark = pytest.mark.django_db


def test_shipmentevent_orm_mutations_are_refused() -> None:
    event = ShipmentEventFactory.create()
    event.bts_status = "999"
    with pytest.raises(AppendOnlyError):
        event.save()
    with pytest.raises(AppendOnlyError):
        event.delete()
    with pytest.raises(AppendOnlyError):
        ShipmentEvent.objects.filter(pk=event.pk).update(bts_status="999")
    with pytest.raises(AppendOnlyError):
        ShipmentEvent.objects.filter(pk=event.pk).delete()
    with pytest.raises(AppendOnlyError):
        ShipmentEvent.objects.bulk_update([event], ["bts_status"])
    event.refresh_from_db()
    assert event.bts_status == "100"


def test_shipmentevent_raw_sql_mutations_are_refused_by_the_trigger() -> None:
    event = ShipmentEventFactory.create()
    with (
        pytest.raises(DatabaseError, match="append-only table delivery_shipmentevent"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE delivery_shipmentevent SET bts_status = '999' WHERE id = %s", [event.pk])
    with (
        pytest.raises(DatabaseError, match="append-only table delivery_shipmentevent"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM delivery_shipmentevent WHERE id = %s", [event.pk])
    assert ShipmentEvent.objects.filter(pk=event.pk, bts_status="100").exists()


def test_shipmentevent_is_not_purgeable_even_inside_the_purge_window() -> None:
    assert "delivery_shipmentevent" not in PURGEABLE_TABLES  # D-4: never purged
    event = ShipmentEventFactory.create()
    with (
        pytest.raises(DatabaseError, match="append-only table delivery_shipmentevent"),
        purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM delivery_shipmentevent WHERE id = %s", [event.pk])
    with (
        pytest.raises(DatabaseError, match="append-only table delivery_shipmentevent"),
        purge_window(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE delivery_shipmentevent SET manual = true WHERE id = %s", [event.pk])
    assert ShipmentEvent.objects.filter(pk=event.pk).exists()


def test_shipmentevent_trigger_in_pg_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname, tgtype FROM pg_trigger WHERE tgrelid = 'delivery_shipmentevent'::regclass"
            " AND NOT tgisinternal"
        )
        rows = cursor.fetchall()
    assert [row[0] for row in rows] == ["delivery_shipmentevent_append_only"]
    # tgtype bits: 1 = ROW, 2 = BEFORE, 8 = DELETE, 16 = UPDATE → BEFORE UPDATE OR DELETE FOR EACH ROW
    assert rows[0][1] & (1 | 2 | 8 | 16) == 1 | 2 | 8 | 16
    # the mutable Shipment table carries no such trigger
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_trigger"
            " WHERE tgrelid = 'delivery_shipment'::regclass AND NOT tgisinternal"
        )
        assert cursor.fetchone() == (0,)
