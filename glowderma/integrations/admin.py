"""Read-only Django admin for the integrations models (design 01 §11)."""

from django.contrib import admin

from glowderma.core.admin import ReadOnlyAdmin, register_read_only
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox


class WebhookInboxAdmin(ReadOnlyAdmin):
    list_display = ("id", "received_at", "provider", "event_key", "entity_ref", "result_status")
    list_filter = ("provider",)
    search_fields = ("event_key", "entity_ref")
    ordering = ("-received_at", "-id")
    date_hierarchy = "received_at"

    @admin.display(description="result status")
    def result_status(self, obj: WebhookInbox) -> str:
        status = obj.result.get("status") if isinstance(obj.result, dict) else None
        return "-" if status is None else str(status)


admin.site.register(WebhookInbox, WebhookInboxAdmin)
register_read_only(
    Job,
    list_display=("id", "kind", "state", "created_at", "started_at", "finished_at", "created_by"),
    list_filter=("kind", "state"),
    ordering=("-created_at", "-id"),
    raw_id_fields=("created_by",),
)
register_read_only(
    IntegrationState,
    list_display=("provider", "last_sync_at", "last_success_at", "last_error_at", "consecutive_failures"),
    ordering=("provider",),
)
