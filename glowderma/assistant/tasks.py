"""Background work of `assistant`: the generation run and the stale-job reaper.

``services.start_job`` announces a job on the outbox topic ``assistant.job_queued`` (after commit); the
handler turns it into the Celery task ``assistant.run_generation(job_id)`` — ids only, never a key or a
prompt. Celery delivers at least once (``ACKS_LATE``): ``services.claim_job`` skips a job whose run is alive
or finished. A run is bounded by its soft limit (90 s, the provider's own timeouts and retries fit within
it); the beat task ``assistant.reap_stale_jobs`` fails a job whose worker was lost.
"""

import logging
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

from glowderma.assistant import credential_tasks, services, topics
from glowderma.assistant.models import GenerationJob
from glowderma.core import outbox
from glowderma.core.models import OutboxMessage

logger = logging.getLogger(__name__)

RUN_SOFT_TIME_LIMIT = 90
RUN_TIME_LIMIT = 120


@outbox.register(topics.JOB_QUEUED)
def on_job_queued(message: OutboxMessage) -> None:
    payload: dict[str, Any] = message.payload if isinstance(message.payload, dict) else {}
    job_id = int(payload["job_id"])
    result = run_generation.delay(job_id)
    GenerationJob.objects.filter(pk=job_id).update(celery_task_id=str(result.id)[:64])


@outbox.register(topics.CREDENTIAL_TEST)
def on_credential_test(message: OutboxMessage) -> None:
    payload: dict[str, Any] = message.payload if isinstance(message.payload, dict) else {}
    version = payload.get("version")
    actor_id = payload.get("actor_id")
    credential_tasks.test_credential.delay(
        int(payload["config_id"]),
        None if version is None else int(version),
        None if actor_id is None else int(actor_id),
    )


@shared_task(  # type: ignore[untyped-decorator]
    name="assistant.run_generation", soft_time_limit=RUN_SOFT_TIME_LIMIT, time_limit=RUN_TIME_LIMIT
)
def run_generation(job_id: int) -> None:
    try:
        services.run_job(job_id)
    except SoftTimeLimitExceeded:
        logger.error("assistant job %s ran out of time (%s s)", job_id, RUN_SOFT_TIME_LIMIT)
        job = GenerationJob.objects.filter(pk=job_id).first()
        if job is not None:
            services.fail_job(job, "provider_unavailable")
    except Exception:
        logger.exception("assistant job %s crashed", job_id)
        job = GenerationJob.objects.filter(pk=job_id).first()
        if job is not None:
            services.fail_job(job, services.SERVER_ERROR_CODE)


@shared_task(name="assistant.reap_stale_jobs")  # type: ignore[untyped-decorator]
def reap_stale_jobs() -> int:
    return len(services.reap_stale_jobs())
