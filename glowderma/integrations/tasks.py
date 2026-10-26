"""Beat tasks of `integrations` (design 01 §2.2): the stale-job reaper and the webhook-inbox retention.

``integrations.reap_stale_jobs`` runs every 5 minutes (``config/celery.py``) and fails any ``running`` job
whose heartbeat is older than :data:`glowderma.integrations.services.STALE_JOB_AFTER` with
``{"code": "worker_lost"}``, so the single-flight index never wedges the import button after a worker loss.
``integrations.purge_webhook_inbox`` runs nightly and deletes the raw provider callbacks older than
``WEBHOOK_INBOX_RETENTION_DAYS`` (docs/ops/compliance.md).
"""

import logging
from datetime import timedelta

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings

from glowderma.core import timeutils
from glowderma.integrations import services

logger = logging.getLogger(__name__)


@shared_task(name="integrations.reap_stale_jobs")  # type: ignore[untyped-decorator]
def reap_stale_jobs() -> int:
    """Fail every stale running job; returns how many were reaped."""
    return len(services.reap_stale_jobs())


@shared_task(name="integrations.purge_webhook_inbox")  # type: ignore[untyped-decorator]
def purge_webhook_inbox() -> int:
    """Nightly retention (beat ``integrations-purge-webhook-inbox``); returns how many rows were deleted."""
    before = timeutils.now() - timedelta(days=int(settings.WEBHOOK_INBOX_RETENTION_DAYS))
    return services.purge_webhook_inbox(before, batch_size=int(settings.RETENTION_BATCH_SIZE))
