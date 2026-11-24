"""Transactional writes of `integrations`: the ``Job`` state machine and the per-provider sync facts.

``queued → running → succeeded | failed`` (design 01 §2.2). Starting a job is single-flight per kind: the
partial unique index ``job_single_flight_per_kind`` rejects a second queued/running row and
:func:`start_job` then returns the active one. The work itself belongs to the orchestrating module (the
Billz import lives in ``catalog.tasks``); it is handed over through the outbox as a string topic, because
``integrations`` sits below every module that could do the work.

The package: ``jobs`` (the background job lifecycle), ``provider_state`` (each provider's
``IntegrationState``) and ``retention`` (purging raw provider callbacks). Callers import this facade.
"""

from glowderma.integrations.services.jobs import (
    ACTIVE_STATES,
    JOB_QUEUED_TOPIC,
    STALE_JOB_AFTER,
    WORKER_LOST_CODE,
    WORKER_LOST_MESSAGE,
    JobNotActive,
    claim_job,
    fail_job,
    finish_job,
    is_lost_in_queue,
    is_stale,
    reap_stale_jobs,
    report_progress,
    set_celery_task_id,
    start_job,
)
from glowderma.integrations.services.provider_state import (
    get_state,
    record_failure,
    record_success,
    record_sync_success,
    set_state_data,
    update_provider_data,
)
from glowderma.integrations.services.retention import (
    PURGE_MAX_SECONDS,
    purge_webhook_inbox,
)

__all__ = [
    "ACTIVE_STATES",
    "JOB_QUEUED_TOPIC",
    "PURGE_MAX_SECONDS",
    "STALE_JOB_AFTER",
    "WORKER_LOST_CODE",
    "WORKER_LOST_MESSAGE",
    "JobNotActive",
    "claim_job",
    "fail_job",
    "finish_job",
    "get_state",
    "is_lost_in_queue",
    "is_stale",
    "purge_webhook_inbox",
    "reap_stale_jobs",
    "record_failure",
    "record_success",
    "record_sync_success",
    "report_progress",
    "set_celery_task_id",
    "set_state_data",
    "start_job",
    "update_provider_data",
]
