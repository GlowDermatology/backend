"""Background work of `catalog`: the Billz import job (contract §2.3 / §2.14).

``integrations.services.start_job`` queues the ``Job`` row and announces it on the outbox topic
``job.queued`` (integrations sits below catalog, so it cannot call this module). The handler here turns
that into the Celery task ``catalog.run_billz_import``; in tests ``CELERY_TASK_ALWAYS_EAGER`` runs it
inline, so an API test sees the finished job after ``django_capture_on_commit_callbacks(execute=True)``.
"""

import logging
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from glowderma.catalog import billz, services
from glowderma.core import audit, enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import MESSAGES
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as job_services
from glowderma.integrations.billz.ports import BillzError
from glowderma.integrations.models import IntegrationState, Job

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 50


@outbox.register(job_services.JOB_QUEUED_TOPIC)
def on_job_queued(message: OutboxMessage) -> None:
    payload: dict[str, Any] = message.payload if isinstance(message.payload, dict) else {}
    if payload.get("kind") != enums.JobKind.BILLZ_IMPORT:
        return
    job_id = int(payload["job_id"])
    result = run_billz_import.delay(job_id)
    job_services.set_celery_task_id(job_id, str(result.id))


def _finished(job: Job, *, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    audit.record(
        action="billz.import_finished",
        entity_type="job",
        entity_id=str(job.pk),
        after={"job_id": job.pk, "state": job.state, "result": result, "error": error},
    )


def _fail(job: Job, code: str, message: str) -> None:
    job_services.record_failure(IntegrationState.Provider.BILLZ, message)
    try:
        failed = job_services.fail_job(job, code, message)
    except job_services.JobNotActive:  # a duplicate delivery already finished it
        return
    _finished(failed, error=failed.error)


@shared_task(name="catalog.run_billz_import")  # type: ignore[untyped-decorator]
def run_billz_import(job_id: int) -> None:
    """Claim the queued job, upsert every Billz row, record the sync and finish the job (never raises)."""
    job = job_services.claim_job(job_id)
    if job is None:
        logger.info("Billz import job %s is not queued; nothing to do", job_id)
        return
    if integration_modes.fake_in_production("billz"):
        # Queued before a switch to fake under the production settings: no port call, last_sync_at unchanged.
        logger.warning("Billz import job %s refused: Billz is fake under the production settings", job_id)
        try:
            failed = job_services.fail_job(job, services.BILLZ_FAKE_MODE_CODE, services.BILLZ_FAKE_MODE)
        except job_services.JobNotActive:
            return
        _finished(failed, error=failed.error)
        return
    mode = (
        str(job.params.get("mode", enums.BillzImportMode.DELTA)) if isinstance(job.params, dict) else "delta"
    )
    counters = {"created": 0, "updated": 0, "unchanged": 0, "new_from_billz": 0}
    try:
        port = billz.get_port()
        rows = list(port.iter_products("full" if mode == enums.BillzImportMode.FULL else "delta"))
        total = len(rows)
        job_services.report_progress(job, 0, total)
        for index, row in enumerate(rows, start=1):
            outcome = services.upsert_from_billz(row)
            counters[outcome] += 1
            if outcome == "created":
                counters["new_from_billz"] += 1
            if index % PROGRESS_EVERY == 0 or index == total:
                job_services.report_progress(job, index, total)
        job_services.record_sync_success(IntegrationState.Provider.BILLZ, synced_at=timeutils.now())
        finished = job_services.finish_job(job, counters)
    except BillzError as exc:
        logger.warning("Billz import job %s failed: %s", job_id, exc)
        _fail(job, "provider_unavailable", MESSAGES["provider_unavailable"])
        return
    except job_services.JobNotActive:
        return
    except Exception:
        logger.exception("Billz import job %s crashed", job_id)
        _fail(job, "server_error", MESSAGES["server_error"])
        return
    _finished(finished, result=counters)
