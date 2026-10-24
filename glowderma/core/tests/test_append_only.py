import pytest
from django.db import DatabaseError, connection, transaction

from glowderma.core.models import AppendOnlyError, AuditEntry

pytestmark = pytest.mark.django_db


def make_entry() -> AuditEntry:
    return AuditEntry.objects.create(
        actor_type="system", action="test.created", entity_type="test", entity_id="1"
    )


def test_append_only_rejects_resave() -> None:
    entry = make_entry()
    entry.reason = "changed"
    with pytest.raises(AppendOnlyError):
        entry.save()
    entry.refresh_from_db()
    assert entry.reason == ""


def test_append_only_rejects_instance_delete() -> None:
    entry = make_entry()
    with pytest.raises(AppendOnlyError):
        entry.delete()
    assert AuditEntry.objects.filter(pk=entry.pk).exists()


def test_append_only_rejects_queryset_update() -> None:
    entry = make_entry()
    with pytest.raises(AppendOnlyError):
        AuditEntry.objects.filter(pk=entry.pk).update(reason="changed")
    entry.refresh_from_db()
    assert entry.reason == ""


def test_append_only_rejects_queryset_delete() -> None:
    entry = make_entry()
    with pytest.raises(AppendOnlyError):
        AuditEntry.objects.filter(pk=entry.pk).delete()
    assert AuditEntry.objects.filter(pk=entry.pk).exists()


def test_append_only_rejects_bulk_update() -> None:
    entry = make_entry()
    entry.reason = "changed"
    with pytest.raises(AppendOnlyError):
        AuditEntry.objects.bulk_update([entry], ["reason"])
    entry.refresh_from_db()
    assert entry.reason == ""


def test_append_only_rejects_raw_sql_update() -> None:
    entry = make_entry()
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_auditentry SET reason = %s WHERE id = %s", ["changed", entry.pk])
    entry.refresh_from_db()
    assert entry.reason == ""
    assert AuditEntry.objects.count() == 1


def test_append_only_rejects_raw_sql_delete() -> None:
    entry = make_entry()
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_auditentry WHERE id = %s", [entry.pk])
    assert AuditEntry.objects.filter(pk=entry.pk).exists()
    assert AuditEntry.objects.count() == 1
