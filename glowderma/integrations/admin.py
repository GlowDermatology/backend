"""Read-only Django admin for the integrations models (design 01 §11)."""

from typing import Any

from django.contrib import admin

from glowderma.core.admin import (
    DANGER,
    INFO,
    PRIMARY,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    date_range,
    display,
    dropdown,
    register_read_only,
    status_column,
)
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox

# ``WebhookInbox.result["outcome"]`` (commerce.api.webhooks.inbox).
WEBHOOK_OUTCOME_VARIANTS = {"accepted": SUCCESS, "duplicate": INFO, "rejected": WARNING, "error": DANGER}
JOB_STATE_VARIANTS = {"queued": INFO, "running": PRIMARY, "succeeded": SUCCESS, "failed": DANGER}


def _result(obj: WebhookInbox) -> dict[str, Any]:
    return obj.result if isinstance(obj.result, dict) else {}


class WebhookInboxAdmin(ReadOnlyAdmin):
    list_display = ("id", "received_at", "provider", "event_key", "entity_ref", "outcome", "result_status")
    list_filter = (dropdown("provider"), date_range("received_at"))
    search_fields = ("event_key", "entity_ref", "request_id")
    ordering = ("-received_at", "-id")
    date_hierarchy = "received_at"

    @display(description="result status")
    def result_status(self, obj: WebhookInbox) -> str:
        status = _result(obj).get("status")
        return "-" if status is None else str(status)

    @display(description="natija", label=WEBHOOK_OUTCOME_VARIANTS)
    def outcome(self, obj: WebhookInbox) -> tuple[str, str]:
        outcome = _result(obj).get("outcome")
        code = _result(obj).get("error_code")
        if not outcome:
            return ("", "-")
        return (str(outcome), f"{outcome} · {code}" if code else str(outcome))


admin.site.register(WebhookInbox, WebhookInboxAdmin)
register_read_only(
    Job,
    list_display=(
        "id",
        "kind",
        status_column("state", "Holat", JOB_STATE_VARIANTS),
        "created_at",
        "started_at",
        "finished_at",
        "created_by",
    ),
    list_filter=(dropdown("kind"), dropdown("state"), date_range("created_at")),
    ordering=("-created_at", "-id"),
    autocomplete_fields=("created_by",),
)
register_read_only(
    IntegrationState,
    list_display=("provider", "last_sync_at", "last_success_at", "last_error_at", "consecutive_failures"),
    ordering=("provider",),
)
