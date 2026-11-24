"""Retention of raw provider callbacks (docs/ops/compliance.md): the nightly purge of the webhook inbox."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from django.db import connection

from glowderma.core import audit
from glowderma.core.db import PURGEABLE_TABLES, purge_window
from glowderma.integrations.models import WebhookInbox

logger = logging.getLogger("glowderma.integrations.services")


_INBOX_TABLE = WebhookInbox._meta.db_table


if _INBOX_TABLE not in PURGEABLE_TABLES:  # the trigger lets a purge through only for these tables
    raise RuntimeError(f"{_INBOX_TABLE} must be listed in core.db.PURGEABLE_TABLES")


# Oldest rows of one provider first. Plan: ``integr_webhook_provider_time`` (provider, received_at) range.
_PURGE_INBOX_SQL = (
    f"DELETE FROM {_INBOX_TABLE} WHERE id IN ("  # noqa: S608 - the table name is a model constant
    f"SELECT id FROM {_INBOX_TABLE} WHERE provider = %s AND received_at < %s ORDER BY received_at LIMIT %s)"
)


# A run stops starting new batches after this long (the Celery soft limit is 540 s); the next night continues.
PURGE_MAX_SECONDS = 300.0


def purge_webhook_inbox(before: datetime, *, batch_size: int, max_seconds: float = PURGE_MAX_SECONDS) -> int:
    """Delete ``WebhookInbox`` rows received before ``before`` — raw provider payloads hold personal data —
    ``batch_size`` rows per transaction, each inside ``core.db.purge_window()`` (the append-only trigger lets
    that transaction's deletes through; nothing else changes). Audits ``integrations.inbox_purged {provider,
    rows, before}`` per provider that lost rows; returns the total."""
    deadline = time.monotonic() + max_seconds
    total = 0
    for provider in WebhookInbox.Provider.values:
        rows = 0
        while time.monotonic() < deadline:
            with purge_window(), connection.cursor() as cursor:
                cursor.execute(_PURGE_INBOX_SQL, [provider, before, batch_size])
                deleted = max(cursor.rowcount, 0)
            rows += deleted
            if deleted < batch_size:
                break
        if rows:
            audit.record(
                action="integrations.inbox_purged",
                entity_type="webhook_inbox",
                entity_id=provider,
                after={"provider": provider, "rows": rows, "before": before},
            )
        total += rows
    logger.info("retention.webhook_inbox_purged rows=%s before=%s", total, before.isoformat())
    return total


__all__ = [
    "PURGE_MAX_SECONDS",
    "purge_webhook_inbox",
]
