"""Read-side helpers of `integrations` (jobs, provider state). Reads never create rows."""

from datetime import datetime
from typing import Any

from django.db.models import Count, Q

from glowderma.core import enums
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox

# ``WebhookInbox.result["outcome"]`` values of a delivery we did not accept (commerce.api.webhooks.inbox).
FAILED_WEBHOOK_OUTCOMES = ("rejected", "error")


def get_job(job_id: int) -> Job | None:
    return Job.objects.select_related("created_by").filter(pk=job_id).first()


def latest_job(kind: str) -> Job | None:
    """The most recent job of ``kind`` by ``created_at`` (any state)."""
    return Job.objects.filter(kind=kind).order_by("-created_at", "-id").first()


def provider_state(provider: str) -> IntegrationState | None:
    return IntegrationState.objects.filter(provider=provider).first()


def provider_data(provider: str) -> dict[str, Any]:
    """A copy of the provider's cursor document (``IntegrationState.data``); ``{}`` without a row."""
    state = provider_state(provider)
    return dict(state.data) if state is not None and isinstance(state.data, dict) else {}


def billz_status(*, configured: bool) -> dict[str, Any]:
    """Contract §2.3 ``products/stats.billz``: ``connected`` = adapter configured ∧ last import not failed."""
    last = latest_job(enums.JobKind.BILLZ_IMPORT)
    state = provider_state(IntegrationState.Provider.BILLZ)
    return {
        "connected": configured and (last is None or last.state != enums.JobState.FAILED),
        "last_sync_at": state.last_sync_at if state is not None else None,
    }


def failed_webhooks_since(since: datetime) -> int:
    """Provider callbacks received at or after ``since`` that were rejected or failed (ops console badge)."""
    return WebhookInbox.objects.filter(
        received_at__gte=since, result__outcome__in=list(FAILED_WEBHOOK_OUTCOMES)
    ).count()


def provider_states() -> dict[str, IntegrationState]:
    """``{provider: IntegrationState}`` of every provider row (one query; the rows always exist)."""
    return {state.provider: state for state in IntegrationState.objects.all()}


def latest_jobs() -> dict[str, Job]:
    """The most recent job of every kind that ever ran, ``{kind: Job}`` (one ``DISTINCT ON`` query)."""
    rows = Job.objects.order_by("kind", "-created_at", "-id").distinct("kind")
    return {job.kind: job for job in rows}


def webhook_outcomes_since(since: datetime) -> dict[str, dict[str, int]]:
    """``{provider: {"total", "failed"}}`` of the callbacks received at or after ``since`` (one query over
    ``received_at``); "failed" is :data:`FAILED_WEBHOOK_OUTCOMES`, the rule of the sidebar badge."""
    rows = (
        WebhookInbox.objects.filter(received_at__gte=since)
        .values("provider")
        .annotate(
            total=Count("id"),
            failed=Count("id", filter=Q(result__outcome__in=list(FAILED_WEBHOOK_OUTCOMES))),
        )
        .order_by()
    )
    return {str(row["provider"]): {"total": int(row["total"]), "failed": int(row["failed"])} for row in rows}
