"""Background work of `catalog`: the Billz import job (contract §2.3 / §2.14).

``integrations.services.start_job`` queues the ``Job`` row and announces it on the outbox topic
``job.queued`` (integrations sits below catalog, so it cannot call this module). The handler here turns
that into the Celery task ``catalog.run_billz_import``; in tests ``CELERY_TASK_ALWAYS_EAGER`` runs it
inline, so an API test sees the finished job after ``django_capture_on_commit_callbacks(execute=True)``.

A run applies every row the port yields, then — for a ``full`` run, whose listing the port guarantees
complete — hides the active products Billz no longer lists (``services.hide_missing_from_billz``; result
key ``hidden``), and only then lets the port advance its cursor (``sync_succeeded``) and records the sync.
A failure anywhere leaves the cursor and ``billz.last_sync_at`` where they were.

Beat (``config/celery.py``): ``catalog.billz_sync_delta`` hourly and ``catalog.billz_sync_full`` daily start
the same ``billz_import`` job through ``start_job`` (``created_by`` = system), so they single-flight with an
admin import. With the fake Billz (any settings) they do nothing at all: the fake would only refresh
``synced_at`` and write a job and two audit rows every hour, and under the production settings a fake import
is refused anyway. A daily full that finds a delta (or an admin import) running retries a few times later
instead of being skipped for the day.
"""

import logging
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from glowderma.catalog import billz, services
from glowderma.core import audit, enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import MESSAGES
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as job_services
from glowderma.integrations.billz.ports import BillzError, ImportMode
from glowderma.integrations.billz.registry import billz_mode
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
    port_mode: ImportMode = "full" if mode == enums.BillzImportMode.FULL else "delta"
    try:
        port = billz.get_port()
        rows = list(port.iter_products(port_mode))
        total = len(rows)
        job_services.report_progress(job, 0, total)
        for index, row in enumerate(rows, start=1):
            outcome = services.upsert_from_billz(row)
            counters[outcome] += 1
            if outcome == "created":
                counters["new_from_billz"] += 1
            if index % PROGRESS_EVERY == 0 or index == total:
                job_services.report_progress(job, index, total)
        if port_mode == "full":
            counters["hidden"] = len(services.hide_missing_from_billz({row.billz_id for row in rows}))
        port.sync_succeeded(port_mode)
        job_services.record_sync_success(IntegrationState.Provider.BILLZ, synced_at=timeutils.now())
        finished = job_services.finish_job(job, counters)
    except BillzError as exc:
        logger.warning("Billz import job %s failed: %s", job_id, exc)
        # the message is the generic catalog one; the code tells "Billz down" from "listing incomplete"
        _fail(job, exc.code, MESSAGES["provider_unavailable"])
        return
    except job_services.JobNotActive:
        return
    except Exception:
        logger.exception("Billz import job %s crashed", job_id)
        _fail(job, "server_error", MESSAGES["server_error"])
        return
    _finished(finished, result=counters)


# --- beat: hourly delta, daily full (docs/integrations/billz.md) -------------------------------------------

# A daily full that meets a running import retries this often, this many times (then waits for tomorrow).
FULL_RETRY_SECONDS = 900
FULL_MAX_RETRIES = 4


def _scheduled_import(mode: str) -> Job | None:
    """Start (or join) the ``billz_import`` job of ``mode``; ``None`` when the beat must do nothing."""
    if billz_mode() != "live":
        logger.debug("Billz %s sync skipped: Billz is fake", mode)
        return None
    if not billz.get_port().is_configured():
        logger.warning("Billz %s sync skipped: the live adapter is not configured", mode)
        return None
    job, created = job_services.start_job(enums.JobKind.BILLZ_IMPORT, {"mode": mode}, actor=None)
    if not created:
        logger.info("Billz %s sync: import job %s is already %s", mode, job.pk, job.state)
    return job


@shared_task(name="catalog.billz_sync_delta")  # type: ignore[untyped-decorator]
def billz_sync_delta() -> int | None:
    """Hourly: the changes since the last successful sync (a running import covers them already)."""
    job = _scheduled_import(enums.BillzImportMode.DELTA)
    return None if job is None else job.pk


@shared_task(name="catalog.billz_sync_full", bind=True, max_retries=FULL_MAX_RETRIES)  # type: ignore[untyped-decorator]
def billz_sync_full(self: Any) -> int | None:
    """Daily: the reconciliation listing. If a delta import holds the single-flight slot, try again later."""
    job = _scheduled_import(enums.BillzImportMode.FULL)
    if job is None:
        return None
    params = job.params if isinstance(job.params, dict) else {}
    if params.get("mode") != enums.BillzImportMode.FULL:
        if self.request.retries >= FULL_MAX_RETRIES:
            logger.warning("Billz full sync skipped today: import job %s kept running", job.pk)
            return None
        raise self.retry(countdown=FULL_RETRY_SECONDS)
    return int(job.pk)
