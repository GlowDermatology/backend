"""Transactional writes of `integrations`: the ``Job`` state machine and the per-provider sync facts.

``queued → running → succeeded | failed`` (design 01 §2.2). Starting a job is single-flight per kind: the
partial unique index ``job_single_flight_per_kind`` rejects a second queued/running row and
:func:`start_job` then returns the active one. The work itself belongs to the orchestrating module (the
Billz import lives in ``catalog.tasks``); it is handed over through the outbox as a string topic, because
``integrations`` sits below every module that could do the work.
"""

import logging
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F

from glowderma.core import audit, enums, outbox, timeutils
from glowderma.integrations.models import IntegrationState, Job

logger = logging.getLogger(__name__)

ACTIVE_STATES = (enums.JobState.QUEUED, enums.JobState.RUNNING)
# Outbox topic of a freshly queued job; the orchestrating module registers the handler in its ``tasks.py``.
JOB_QUEUED_TOPIC = "job.queued"
# A running job whose worker has not touched ``heartbeat_at`` for this long lost its worker (design 01 §2.2):
# a Celery redelivery may re-claim it, and ``tasks.reap_stale_jobs`` fails it so the single-flight index
# never wedges the import button.
STALE_JOB_AFTER = timedelta(minutes=15)
WORKER_LOST_CODE = "worker_lost"
WORKER_LOST_MESSAGE = "Jarayon uzilib qoldi. Qayta urinib ko‘ring."


class JobNotActive(RuntimeError):
    """A worker tried to move a job that is no longer queued/running (a duplicate delivery)."""


def start_job(
    kind: str, params: dict[str, Any], *, actor: Any = None, request: Any = None
) -> tuple[Job, bool]:
    """Queue a job of ``kind`` or return the one already queued/running (single-flight).

    Returns ``(job, created)``. A created job is announced on the outbox topic ``job.queued`` with
    ``{"job_id", "kind"}`` once the surrounding transaction commits; the ``billz_import`` kind also writes the
    ``billz.import_started`` audit row (contract §2.3).
    """
    with transaction.atomic():
        try:
            with transaction.atomic():
                job = Job.objects.create(kind=kind, params=params, created_by=actor)
        except IntegrityError:
            active = (
                Job.objects.filter(kind=kind, state__in=ACTIVE_STATES).order_by("-created_at", "-id").first()
            )
            if active is None:  # the competing job finished between the two statements: queue ours again
                job = Job.objects.create(kind=kind, params=params, created_by=actor)
            else:
                return active, False
        if kind == enums.JobKind.BILLZ_IMPORT:
            audit.record(
                action="billz.import_started",
                entity_type="job",
                entity_id=str(job.pk),
                actor=actor,
                request=request,
                after={"job_id": job.pk, "params": params},
            )
        outbox.enqueue(JOB_QUEUED_TOPIC, {"job_id": job.pk, "kind": kind}, dedupe_key=f"job:{job.pk}")
        return job, True


def is_stale(job: Job, *, now: Any = None) -> bool:
    """A ``running`` job whose heartbeat is older than :data:`STALE_JOB_AFTER` (its worker was lost)."""
    if job.state != enums.JobState.RUNNING:
        return False
    reference = job.heartbeat_at or job.started_at or job.created_at
    return bool(reference < (now or timeutils.now()) - STALE_JOB_AFTER)


def claim_job(job_id: int) -> Job | None:
    """Move a queued job to ``running`` (``attempts += 1``); ``None`` when it cannot be claimed.

    A ``running`` job whose heartbeat is stale (:func:`is_stale`) is re-claimed the same way, keeping its
    ``started_at``: that is a Celery redelivery after a worker loss (``acks_late`` +
    ``task_reject_on_worker_lost``) resuming the idempotent work instead of leaving the row stuck.
    """
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        instant = timeutils.now()
        if job.state == enums.JobState.QUEUED:
            job.started_at = instant
        elif not is_stale(job, now=instant):
            return None
        job.state = enums.JobState.RUNNING
        job.heartbeat_at = instant
        job.attempts += 1
        job.save(update_fields=["state", "started_at", "heartbeat_at", "attempts"])
        return job


def reap_stale_jobs(*, now: Any = None) -> list[Job]:
    """Fail every stale running job with ``{"code": "worker_lost"}`` (design 01 §2.2); returns them.

    Each job is finished in its own transaction under the row lock, so a worker that is still alive and
    heartbeats concurrently wins (the row is re-checked after the lock). A ``billz_import`` job also records
    the provider failure and the ``billz.import_finished`` audit row, like the worker would.
    """
    instant = now or timeutils.now()
    reaped: list[Job] = []
    candidates = Job.objects.filter(
        state=enums.JobState.RUNNING, heartbeat_at__lt=instant - STALE_JOB_AFTER
    ).values_list("pk", flat=True)
    for job_id in list(candidates):
        with transaction.atomic():
            job = Job.objects.select_for_update().filter(pk=job_id).first()
            if job is None or not is_stale(job, now=instant):
                continue
            job = _finish(
                job, enums.JobState.FAILED, error={"code": WORKER_LOST_CODE, "message": WORKER_LOST_MESSAGE}
            )
            if job.kind == enums.JobKind.BILLZ_IMPORT:
                record_failure(IntegrationState.Provider.BILLZ, WORKER_LOST_MESSAGE)
                audit.record(
                    action="billz.import_finished",
                    entity_type="job",
                    entity_id=str(job.pk),
                    after={"job_id": job.pk, "state": job.state, "result": None, "error": job.error},
                )
        logger.warning("Reaped stale %s job %s (no heartbeat since %s)", job.kind, job.pk, job.heartbeat_at)
        reaped.append(job)
    return reaped


def set_celery_task_id(job_id: int, task_id: str) -> None:
    Job.objects.filter(pk=job_id).update(celery_task_id=task_id[:36])


def report_progress(job: Job, done: int, total: int | None) -> None:
    job.progress = None if total is None else {"done": done, "total": total}
    job.heartbeat_at = timeutils.now()
    job.save(update_fields=["progress", "heartbeat_at"])


def _finish(job: Job, state: str, *, result: Any = None, error: Any = None) -> Job:
    with transaction.atomic():
        current = Job.objects.select_for_update().get(pk=job.pk)
        if current.state != enums.JobState.RUNNING:
            raise JobNotActive(f"job {job.pk} is {current.state}, not running")
        instant = timeutils.now()
        current.state = state
        current.result = result
        current.error = error
        current.finished_at = instant
        current.heartbeat_at = instant
        current.progress = job.progress
        current.save(update_fields=["state", "result", "error", "finished_at", "heartbeat_at", "progress"])
        return current


def finish_job(job: Job, result: dict[str, Any]) -> Job:
    return _finish(job, enums.JobState.SUCCEEDED, result=result)


def fail_job(job: Job, code: str, message: str) -> Job:
    return _finish(job, enums.JobState.FAILED, error={"code": code, "message": message})


def get_state(provider: str) -> IntegrationState:
    """The provider's state row (created lazily; the migration and ``post_migrate`` normally keep it)."""
    state, _ = IntegrationState.objects.get_or_create(provider=provider)
    return state


def record_sync_success(provider: str, *, synced_at: Any = None) -> IntegrationState:
    """A successful sync (Billz) or poll (BTS): sets ``last_sync_at`` and clears the failure run."""
    instant = synced_at or timeutils.now()
    state = get_state(provider)
    IntegrationState.objects.filter(pk=state.pk).update(
        last_sync_at=instant,
        last_success_at=instant,
        consecutive_failures=0,
        last_error="",
        updated_at=timeutils.now(),
    )
    state.refresh_from_db()
    return state


def record_failure(provider: str, message: str) -> IntegrationState:
    state = get_state(provider)
    instant = timeutils.now()
    IntegrationState.objects.filter(pk=state.pk).update(
        last_error_at=instant,
        last_error=message[:2000],
        consecutive_failures=F("consecutive_failures") + 1,
        updated_at=instant,
    )
    state.refresh_from_db()
    return state
