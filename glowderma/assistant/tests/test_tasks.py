"""The Celery side: time limit and crash handling, the reaper task, photo bytes from the stored variant."""

from __future__ import annotations

from datetime import timedelta

import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.assistant import services, tasks
from glowderma.assistant.models import GenerationJob
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory, image_bytes
from glowderma.core import enums, timeutils
from glowderma.core.variants import store_variants

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes", "assistant_on")]


def queued() -> GenerationJob:
    product = ProductFactory.create(name_uz="Serum", status=enums.ProductStatus.HIDDEN)
    result = services.start_job(
        product.pk, mode="fill_missing", languages=["ru"], actor=StaffUserFactory.create()
    )
    assert result.job is not None
    return result.job


@pytest.mark.parametrize(
    ("error", "code"),
    [(SoftTimeLimitExceeded(), "provider_unavailable"), (RuntimeError("boom"), "server_error")],
)
def test_a_run_that_times_out_or_crashes_fails_the_job(
    error: BaseException, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = queued()

    def claim_then_raise(job_id: int) -> None:
        services.claim_job(job_id)
        raise error

    monkeypatch.setattr(services, "run_job", claim_then_raise)
    tasks.run_generation(job.pk)
    job.refresh_from_db()
    assert job.state == enums.JobState.FAILED and job.error_code == code


def test_the_outbox_handler_queues_the_task_with_the_id_only(fake_llm: object) -> None:
    job = queued()
    message = type("Message", (), {"payload": {"job_id": job.pk}})()
    tasks.on_job_queued(message)
    job.refresh_from_db()
    assert job.state == enums.JobState.SUCCEEDED and job.celery_task_id


def test_reaper_task() -> None:
    job = queued()
    GenerationJob.objects.filter(pk=job.pk).update(created_at=timeutils.now() - timedelta(minutes=10))
    assert tasks.reap_stale_jobs() == 1


def test_photo_bytes_come_from_the_stored_variant() -> None:
    image = ProductImageFactory.create(content=image_bytes())
    store_variants(image.file.storage, str(image.file.name), image.file.read())
    part = services.image_part(image)
    assert part.mime_type == "image/webp" and part.data[:4] == b"RIFF"
