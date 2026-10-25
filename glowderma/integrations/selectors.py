"""Read-side helpers of `integrations` (jobs, provider state). Reads never create rows."""

from typing import Any

from glowderma.core import enums
from glowderma.integrations.models import IntegrationState, Job


def get_job(job_id: int) -> Job | None:
    return Job.objects.select_related("created_by").filter(pk=job_id).first()


def latest_job(kind: str) -> Job | None:
    """The most recent job of ``kind`` by ``created_at`` (any state)."""
    return Job.objects.filter(kind=kind).order_by("-created_at", "-id").first()


def provider_state(provider: str) -> IntegrationState | None:
    return IntegrationState.objects.filter(provider=provider).first()


def billz_status(*, configured: bool) -> dict[str, Any]:
    """Contract §2.3 ``products/stats.billz``: ``connected`` = adapter configured ∧ last import not failed."""
    last = latest_job(enums.JobKind.BILLZ_IMPORT)
    state = provider_state(IntegrationState.Provider.BILLZ)
    return {
        "connected": configured and (last is None or last.state != enums.JobState.FAILED),
        "last_sync_at": state.last_sync_at if state is not None else None,
    }
