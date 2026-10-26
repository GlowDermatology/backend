"""Retention of ``core`` rows (docs/ops/compliance.md, "Retention"): done outbox messages.

``done`` outbox messages are delivery bookkeeping; their payloads carry ids (and, for a few topics, FCM tokens
of deactivated devices), so they are kept ``OUTBOX_RETENTION_DAYS`` and then deleted in batches by the beat
task ``core.purge_outbox``.

``dead`` messages are **never** purged here: each is an action item until staff replay it (``manage.py
requeue_outbox --dead``, which needs the row) or close it as handled by hand (``requeue_outbox --dead
--resolved``: it becomes ``done`` and ages out), and the consistency report reads them without a look-back
(K1 ``k1_billz_sale_dead``; K6 ``k6_outbox_dead`` counts the recent ones). The one exception is ``sms.send``
(phone and code in the payload), which ``accounts.cleanup_auth`` deletes done or dead after 24 h.

The outbox is not an append-only table: the ORM deletes it, which also clears
``delivery.Shipment.create_message`` (``SET_NULL``) of the rows it removes. The audit log (``AuditEntry``) is
never purged: its legal retention is an open question (docs/ops/compliance.md).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from django.db import transaction

from glowderma.core import audit
from glowderma.core.models import OutboxMessage

logger = logging.getLogger(__name__)

# Only delivered messages: a dead one stays until staff resolve it (module doc).
PURGED_OUTBOX_STATUSES: tuple[str, ...] = ("done",)
# A run stops starting new batches after this long (the Celery soft limit is 540 s); the next night continues.
MAX_RUN_SECONDS = 300.0


def purge_outbox_messages(before: datetime, *, batch_size: int, max_seconds: float = MAX_RUN_SECONDS) -> int:
    """Delete ``done`` messages last changed before ``before``, ``batch_size`` per transaction;
    returns how many were deleted and audits ``outbox.purged {rows, before}`` when any were.

    Plan: the ``available_at`` index, range ``available_at < before`` — implied, because a message is claimed
    only once it is available and finishing it stamps ``updated_at``, so a finished message has
    ``available_at <= updated_at`` — oldest first; each batch re-checks the status, so a message requeued
    between the two statements (``manage.py requeue_outbox``) is kept."""
    deadline = time.monotonic() + max_seconds
    total = 0
    while time.monotonic() < deadline:
        ids = list(
            OutboxMessage.objects.filter(
                status__in=PURGED_OUTBOX_STATUSES, available_at__lt=before, updated_at__lt=before
            )
            .order_by("available_at")
            .values_list("pk", flat=True)[:batch_size]
        )
        if not ids:
            break
        with transaction.atomic():
            _count, per_model = OutboxMessage.objects.filter(
                pk__in=ids, status__in=PURGED_OUTBOX_STATUSES, updated_at__lt=before
            ).delete()
        total += per_model.get(OutboxMessage._meta.label, 0)
        if len(ids) < batch_size:
            break
    if total:
        audit.record(
            action="outbox.purged",
            entity_type="outbox",
            entity_id="retention",
            after={"rows": total, "before": before},
        )
    logger.info("retention.outbox_purged rows=%s before=%s", total, before.isoformat())
    return total


__all__ = ["PURGED_OUTBOX_STATUSES", "MAX_RUN_SECONDS", "purge_outbox_messages"]
