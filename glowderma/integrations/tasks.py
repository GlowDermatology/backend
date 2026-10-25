"""Beat tasks of `integrations` (design 01 §2.2): the stale-job reaper.

``integrations.reap_stale_jobs`` runs every 5 minutes (``config/celery.py``) and fails any ``running`` job
whose heartbeat is older than :data:`glowderma.integrations.services.STALE_JOB_AFTER` with
``{"code": "worker_lost"}``, so the single-flight index never wedges the import button after a worker loss.
"""

import logging

from celery import shared_task  # type: ignore[import-untyped]

from glowderma.integrations import services

logger = logging.getLogger(__name__)


@shared_task(name="integrations.reap_stale_jobs")  # type: ignore[untyped-decorator]
def reap_stale_jobs() -> int:
    """Fail every stale running job; returns how many were reaped."""
    return len(services.reap_stale_jobs())
