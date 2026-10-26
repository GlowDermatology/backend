"""Retention of raw provider callbacks (integrations.purge_webhook_inbox; docs/ops/compliance.md).

``WebhookInbox`` is append-only: the purge deletes only inside ``core.db.purge_window()`` and the trigger
keeps refusing every other delete."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import override_settings

from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.integrations import services
from glowderma.integrations.models import WebhookInbox
from glowderma.integrations.tasks import purge_webhook_inbox
from glowderma.integrations.tests.factories import WebhookInboxFactory

pytestmark = pytest.mark.django_db

NOW = datetime.fromisoformat("2026-10-27T03:25:00+05:00")


def inbox(provider: str, age_days: float) -> WebhookInbox:
    return WebhookInboxFactory.create(provider=provider, received_at=NOW - timedelta(days=age_days))


def test_rows_older_than_the_retention_are_purged_per_provider_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: NOW)
    old = [inbox("click", 91 + n) for n in range(3)] + [inbox("payme", 200), inbox("bts", 95)]
    kept = [inbox("click", 89), inbox("payme", 1), inbox("billz", 10)]
    with override_settings(WEBHOOK_INBOX_RETENTION_DAYS=90, RETENTION_BATCH_SIZE=100):
        assert purge_webhook_inbox.apply().get() == len(old)
    assert set(WebhookInbox.objects.values_list("pk", flat=True)) == {row.pk for row in kept}
    before = (NOW - timedelta(days=90)).isoformat()
    rows = {
        entry.entity_id: entry.after
        for entry in AuditEntry.objects.filter(action="integrations.inbox_purged").order_by("id")
    }
    assert rows == {
        "click": {"provider": "click", "rows": 3, "before": before},
        "payme": {"provider": "payme", "rows": 1, "before": before},
        "bts": {"provider": "bts", "rows": 1, "before": before},
    }
    assert AuditEntry.objects.filter(action="integrations.inbox_purged", actor_type="system").count() == 3


def test_batches_continue_until_a_short_one() -> None:
    for n in range(5):
        inbox("click", 100 + n)
    assert services.purge_webhook_inbox(NOW - timedelta(days=90), batch_size=2) == 5
    assert not WebhookInbox.objects.exists()


def test_a_run_stops_starting_batches_at_its_time_limit() -> None:
    inbox("click", 100)
    assert services.purge_webhook_inbox(NOW - timedelta(days=90), batch_size=2, max_seconds=0) == 0
    assert WebhookInbox.objects.count() == 1


def test_the_append_only_guarantee_holds_outside_the_purge() -> None:
    row = inbox("click", 400)
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM integrations_webhookinbox WHERE id = %s", [row.pk])
    assert services.purge_webhook_inbox(NOW - timedelta(days=90), batch_size=10) == 1


def test_the_purge_is_scheduled_nightly_and_the_default_is_90_days() -> None:
    from django.conf import settings

    from config.celery import app

    entry = app.conf.beat_schedule["integrations-purge-webhook-inbox"]
    assert entry["task"] == "integrations.purge_webhook_inbox" == purge_webhook_inbox.name
    assert (entry["schedule"].hour, entry["schedule"].minute) == ({3}, {25})
    assert settings.WEBHOOK_INBOX_RETENTION_DAYS == 180 and settings.OUTBOX_RETENTION_DAYS == 30
