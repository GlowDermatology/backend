"""Reads of `assistant`."""

from __future__ import annotations

from glowderma.assistant.models import GenerationJob
from glowderma.core import enums


def get_job(job_id: int) -> GenerationJob | None:
    """An admin panel job (console prompt tests are not served by the admin API)."""
    return (
        GenerationJob.objects.select_related("created_by")
        .exclude(mode=enums.AssistantMode.TEST)
        .filter(pk=job_id)
        .first()
    )
