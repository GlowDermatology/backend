"""Retention of done outbox messages (dead ones are kept) (core.retention, beat ``core-purge-outbox``;
docs/ops/compliance.md)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.test import override_settings

from glowderma.core import retention, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import purge_outbox

pytestmark = pytest.mark.django_db

NOW = datetime.fromisoformat("2026-10-27T03:20:00+05:00")


def message(key: str, status: str, age_days: float, **extra: object) -> OutboxMessage:
    instant = NOW - timedelta(days=age_days)
    row = OutboxMessage.objects.create(
        topic="push.order_status", payload={}, dedupe_key=key, status=status, **extra
    )
    OutboxMessage.objects.filter(pk=row.pk).update(
        created_at=instant, available_at=instant, updated_at=instant
    )
    return row


def test_finished_messages_older_than_the_retention_are_deleted_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: NOW)
    old = [message(f"done:{n}", "done", 31 + n) for n in range(5)]
    kept = [
        message("done:recent", "done", 29),
        message("dead:recent", "dead", 1),
        message("dead:old", "dead", 400),  # an unresolved action item: never purged, whatever its age
        message("pending:old", "pending", 60),  # still open: never purged, whatever its age
        message("failed:old", "failed", 60),
        message("in_flight:old", "in_flight", 60),
    ]
    with override_settings(OUTBOX_RETENTION_DAYS=30, RETENTION_BATCH_SIZE=2):
        assert purge_outbox.apply().get() == len(old)
    assert set(OutboxMessage.objects.values_list("dedupe_key", flat=True)) == {row.dedupe_key for row in kept}
    entry = AuditEntry.objects.get(action="outbox.purged")
    assert (entry.actor_type, entry.entity_type, entry.entity_id) == ("system", "outbox", "retention")
    assert entry.after == {"rows": 5, "before": (NOW - timedelta(days=30)).isoformat()}
    assert purge_outbox.apply().get() == 0
    assert AuditEntry.objects.filter(action="outbox.purged").count() == 1  # nothing purged: no audit row


def test_a_purged_create_message_is_detached_from_its_shipment() -> None:
    from glowderma.delivery.tests.factories import ShipmentFactory, create_message_for

    shipment = ShipmentFactory.create(failed=True)
    sent = create_message_for(shipment, status="done")
    OutboxMessage.objects.filter(pk=sent.pk).update(
        available_at=NOW - timedelta(days=40), updated_at=NOW - timedelta(days=40)
    )
    assert retention.purge_outbox_messages(NOW - timedelta(days=30), batch_size=100) == 1
    shipment.refresh_from_db()
    assert shipment.create_message_id is None and not OutboxMessage.objects.filter(pk=sent.pk).exists()


def test_a_message_requeued_between_select_and_delete_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    row = message("done:requeued", "done", 40)
    real_filter = OutboxMessage.objects.filter
    calls = {"n": 0}

    def filter_then_requeue(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:  # the delete statement of the first batch
            OutboxMessage.objects.all().update(status="pending")
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(OutboxMessage.objects, "filter", filter_then_requeue)
    assert retention.purge_outbox_messages(NOW - timedelta(days=30), batch_size=100) == 0
    monkeypatch.undo()
    assert OutboxMessage.objects.get(pk=row.pk).status == "pending"


def test_a_run_stops_starting_batches_at_its_time_limit() -> None:
    for n in range(3):
        message(f"done:{n}", "done", 40)
    assert retention.purge_outbox_messages(NOW - timedelta(days=30), batch_size=100, max_seconds=0) == 0
    assert OutboxMessage.objects.count() == 3


def test_the_purge_is_scheduled_nightly() -> None:
    from config.celery import app

    entry = app.conf.beat_schedule["core-purge-outbox"]
    assert entry["task"] == "core.purge_outbox" == purge_outbox.name
    assert (entry["schedule"].hour, entry["schedule"].minute) == ({3}, {20})


@pytest.mark.django_db
def test_the_partial_indexes_of_the_checks_exist() -> None:
    """core.0010 and payments.0003 (built concurrently): the unfinished outbox rows and the open late
    captures, which the consistency checks and the dispatcher read without walking the whole tables."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN (%s, %s)",
            ["outbox_unfinished_idx", "payment_late_capture_open_idx"],
        )
        definitions = dict(cursor.fetchall())
    unfinished = definitions["outbox_unfinished_idx"]
    assert "(status, available_at) WHERE (NOT ((status)::text = 'done'::text))" in unfinished
    late = definitions["payment_late_capture_open_idx"]
    assert "(confirmed_at)" in late and "'late_capture'" in late and "'paid'" in late
