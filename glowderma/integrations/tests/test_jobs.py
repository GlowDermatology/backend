"""Job lifecycle services (design 01 §2.2): claim, stale re-claim, the ``reap_stale_jobs`` beat task.

``Job`` is single-flight per kind (and ``billz_import`` is the only kind), so every test keeps at most one
queued/running row at a time.
"""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.integrations import services, tasks
from glowderma.integrations.models import IntegrationState, Job
from glowderma.integrations.tests.factories import JobFactory

pytestmark = pytest.mark.django_db

WORKER_LOST = {"code": "worker_lost", "message": "Jarayon uzilib qoldi. Qayta urinib ko‘ring."}


def running_job(*, heartbeat_age: timedelta, **extra: Any) -> Job:
    started = timeutils.now() - heartbeat_age - timedelta(minutes=1)
    return JobFactory.create(
        state=enums.JobState.RUNNING,
        started_at=started,
        heartbeat_at=timeutils.now() - heartbeat_age,
        attempts=1,
        **extra,
    )


def test_claim_queued_job_sets_started_and_attempts() -> None:
    job = JobFactory.create()
    claimed = services.claim_job(job.pk)
    assert claimed is not None and claimed.state == "running"
    assert claimed.started_at == claimed.heartbeat_at and claimed.attempts == 1
    assert services.claim_job(job.pk) is None  # a fresh running job is someone else's
    assert services.claim_job(999999) is None


def test_claim_reclaims_a_stale_running_job_keeping_started_at() -> None:
    """A Celery redelivery after a worker loss resumes the job instead of leaving it stuck in ``running``."""
    job = running_job(heartbeat_age=timedelta(hours=5))
    before = timeutils.now()
    claimed = services.claim_job(job.pk)
    assert claimed is not None
    assert claimed.state == "running" and claimed.attempts == 2
    assert claimed.started_at == job.started_at
    assert claimed.heartbeat_at is not None and claimed.heartbeat_at >= before
    assert services.claim_job(job.pk) is None  # now fresh again: a second delivery is a no-op


def test_claim_leaves_a_running_job_under_the_threshold_alone() -> None:
    fresh = running_job(heartbeat_age=timedelta(minutes=14))
    assert services.claim_job(fresh.pk) is None
    fresh.refresh_from_db()
    assert fresh.attempts == 1


def test_is_stale_only_for_running_jobs_past_the_threshold() -> None:
    assert services.STALE_JOB_AFTER == timedelta(minutes=15)
    queued = JobFactory.create()
    assert not services.is_stale(queued)
    queued.state = enums.JobState.RUNNING  # not saved: the predicate is pure
    queued.heartbeat_at = timeutils.now() - timedelta(minutes=16)
    assert services.is_stale(queued)
    queued.heartbeat_at = timeutils.now() - timedelta(minutes=14)
    assert not services.is_stale(queued)
    queued.heartbeat_at = None  # falls back to started_at, then created_at
    queued.started_at = timeutils.now() - timedelta(minutes=16)
    assert services.is_stale(queued)
    assert not services.is_stale(queued, now=queued.started_at + timedelta(minutes=1))


def test_reap_stale_jobs_fails_the_stale_running_job(django_capture_on_commit_callbacks: Any) -> None:
    stale = running_job(heartbeat_age=timedelta(minutes=16))
    with django_capture_on_commit_callbacks(execute=True):
        reaped = tasks.reap_stale_jobs()
    assert reaped == 1
    stale.refresh_from_db()
    assert stale.state == "failed" and stale.finished_at is not None
    assert stale.error == WORKER_LOST
    assert stale.started_at is not None  # kept: the check constraint needs it on a finished row
    entry = AuditEntry.objects.get(action="billz.import_finished")
    assert entry.actor_type == "system" and entry.entity_id == str(stale.pk)
    assert entry.after == {"job_id": stale.pk, "state": "failed", "result": None, "error": WORKER_LOST}
    state = IntegrationState.objects.get(provider="billz")
    assert state.consecutive_failures == 1 and state.last_error == WORKER_LOST["message"]
    # the import can be started again: the single-flight index no longer holds the stale row
    job, created = services.start_job(enums.JobKind.BILLZ_IMPORT, {"mode": "delta"})
    assert created and job.pk != stale.pk and job.state == "queued"
    assert tasks.reap_stale_jobs() == 0  # a queued job is never reaped
    job.refresh_from_db()
    assert job.state == "queued"


def test_reap_stale_jobs_leaves_a_live_running_job_alone() -> None:
    fresh = running_job(heartbeat_age=timedelta(minutes=14))
    assert tasks.reap_stale_jobs() == 0
    fresh.refresh_from_db()
    assert fresh.state == "running"
    assert not AuditEntry.objects.filter(action="billz.import_finished").exists()


def test_reap_stale_jobs_rechecks_under_the_row_lock() -> None:
    stale = running_job(heartbeat_age=timedelta(minutes=16))
    # the worker heartbeats between the candidate scan and the lock: not stale any more → untouched
    original = services.is_stale
    calls: list[int] = []

    def heartbeat_then_check(job: Job, *, now: Any = None) -> bool:
        if not calls:
            Job.objects.filter(pk=job.pk).update(heartbeat_at=timeutils.now())
            job.heartbeat_at = timeutils.now()
        calls.append(job.pk)
        return original(job, now=now)

    with patch.object(services, "is_stale", heartbeat_then_check):
        assert services.reap_stale_jobs() == []
    stale.refresh_from_db()
    assert stale.state == "running" and calls == [stale.pk]


def test_reaper_is_registered_and_scheduled_every_five_minutes() -> None:
    from config.celery import app

    app.autodiscover_tasks(force=True)
    assert "integrations.reap_stale_jobs" in app.tasks
    schedule = app.conf.beat_schedule["reap-stale-jobs"]
    assert schedule == {"task": "integrations.reap_stale_jobs", "schedule": 300.0}


def test_reap_stale_jobs_fails_a_job_no_worker_claimed_in_time(
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Review B14: a queued job whose ``job.queued`` message died (or whose task was lost) would hold the
    single-flight slot for ever, and the beat would join it silently every hour."""
    lost = JobFactory.create()
    Job.objects.filter(pk=lost.pk).update(created_at=timeutils.now() - timedelta(minutes=16))
    young = timeutils.now()
    with django_capture_on_commit_callbacks(execute=True):
        reaped = services.reap_stale_jobs()
    assert [job.pk for job in reaped] == [lost.pk]
    lost.refresh_from_db()
    assert (lost.state, lost.error) == ("failed", WORKER_LOST)
    assert lost.started_at is not None and lost.started_at >= young and lost.finished_at is not None
    assert AuditEntry.objects.filter(action="billz.import_finished", entity_id=str(lost.pk)).exists()
    assert services.claim_job(lost.pk) is None  # a late task finds it finished
    assert not services.is_lost_in_queue(lost)
