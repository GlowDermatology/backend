"""The background job lifecycle (design 01 §2.2): start, claim, progress, finish or fail, and the reaping of
jobs whose worker was lost."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q

from glowderma.core import audit, enums, outbox, timeutils
from glowderma.core.uzsource import N_
from glowderma.integrations.models import IntegrationState, Job
from glowderma.integrations.services.provider_state import record_failure

logger = logging.getLogger("glowderma.integrations.services")


ACTIVE_STATES = (enums.JobState.QUEUED, enums.JobState.RUNNING)


# Outbox topic of a freshly queued job; the orchestrating module registers the handler in its ``tasks.py``.
JOB_QUEUED_TOPIC = "job.queued"


# A running job whose worker has not touched ``heartbeat_at`` for this long lost its worker (design 01 §2.2):
# a Celery redelivery may re-claim it, and ``tasks.reap_stale_jobs`` fails it so the single-flight index
# never wedges the import button.
STALE_JOB_AFTER = timedelta(minutes=15)


WORKER_LOST_CODE = "worker_lost"


WORKER_LOST_MESSAGE = N_("Jarayon uzilib qoldi. Qayta urinib ko‘ring.")


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


def is_lost_in_queue(job: Job, *, now: Any = None) -> bool:
    """A ``queued`` job no worker claimed within :data:`STALE_JOB_AFTER` (its ``job.queued`` message died or
    its Celery task was lost): it would hold the single-flight slot for ever (review B14)."""
    return job.state == enums.JobState.QUEUED and job.created_at < (now or timeutils.now()) - STALE_JOB_AFTER


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
    """Fail every stale running job — and every job still queued after :data:`STALE_JOB_AFTER` — with
    ``{"code": "worker_lost"}`` (design 01 §2.2); returns them.

    Each job is finished in its own transaction under the row lock, so a worker that is still alive and
    heartbeats (or claims the queued job) concurrently wins (the row is re-checked after the lock). A
    ``billz_import`` job also records the provider failure and the ``billz.import_finished`` audit row, like
    the worker would. A late task of a reaped queued job finds it failed and does nothing.
    """
    instant = now or timeutils.now()
    reaped: list[Job] = []
    candidates = Job.objects.filter(
        Q(state=enums.JobState.RUNNING, heartbeat_at__lt=instant - STALE_JOB_AFTER)
        | Q(state=enums.JobState.QUEUED, created_at__lt=instant - STALE_JOB_AFTER)
    ).values_list("pk", flat=True)
    for job_id in list(candidates):
        with transaction.atomic():
            job = Job.objects.select_for_update().filter(pk=job_id).first()
            if job is None or not (is_stale(job, now=instant) or is_lost_in_queue(job, now=instant)):
                continue
            if job.state == enums.JobState.QUEUED:
                # _finish moves running jobs only, and a finished job needs a start (never started: the reap)
                job.state = enums.JobState.RUNNING
                job.started_at = instant
                job.save(update_fields=["state", "started_at"])
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
        logger.warning(
            "Reaped stale %s job %s (no heartbeat since %s, queued at %s)",
            job.kind,
            job.pk,
            job.heartbeat_at,
            job.created_at,
        )
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


__all__ = [
    "ACTIVE_STATES",
    "JOB_QUEUED_TOPIC",
    "STALE_JOB_AFTER",
    "WORKER_LOST_CODE",
    "WORKER_LOST_MESSAGE",
    "JobNotActive",
    "claim_job",
    "fail_job",
    "finish_job",
    "is_lost_in_queue",
    "is_stale",
    "reap_stale_jobs",
    "report_progress",
    "set_celery_task_id",
    "start_job",
]
