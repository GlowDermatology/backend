"""Read-only Django admin for the integrations models (design 01 §11), with two safe audited actions on the
jobs and provider-state lists (glowderma.core.admin_actions):

* «Billz sinxronlashni ishga tushirish» — a ``delta`` Billz import through ``services.start_job``, exactly as
  ``POST integrations/billz/import/`` does (single-flight: a queued or running import is joined, never
  doubled; refused while Billz is fake under the production settings);
* «BTS holatini so‘rash» — enqueues ``commerce.poll_bts`` (by its task name: ``commerce`` sits above this
  module), at most once per :data:`BTS_POLL_COOLDOWN` seconds; the task's own ``cache.add`` lock keeps two
  runs from overlapping.

Webhook inbox rows get no "re-process" action: providers deliver at least once and retry by themselves, a
Click/Payme callback replayed by staff would run a payment state change outside the provider's protocol, the
BTS webhook only re-tracks a parcel (what «BTS holatini so‘rash» does for every stale parcel), and a
``rejected`` row failed authentication — it must never be processed.
"""

from typing import Any

from django.contrib import admin
from django.core.cache import cache
from django.http import HttpRequest

from glowderma.core import enums, integration_modes
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
    status_column,
)
from glowderma.core.admin_actions import (
    ActionResult,
    ConfirmForm,
    OpsActionsMixin,
    done,
    enqueue_task,
    failed,
    ops_action,
    refused,
)
from glowderma.integrations import services
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
BTS_POLL_TASK = "commerce.poll_bts"
BTS_POLL_COOLDOWN = 120
BTS_POLL_COOLDOWN_KEY = "ops:bts_poll"
BILLZ_FAKE = "Billz soxta rejimda ishlayapti: sinxronlash o‘chirilgan."
BTS_FAKE = "BTS soxta rejimda ishlayapti: holat so‘rovi o‘chirilgan."


def _billz_refusal(model_admin: Any, request: HttpRequest, obj: None) -> str:
    if integration_modes.fake_in_production("billz"):
        return BILLZ_FAKE
    active = Job.objects.filter(kind=enums.JobKind.BILLZ_IMPORT, state__in=services.ACTIVE_STATES).first()
    if active is not None:
        return f"Billz importi allaqachon navbatda yoki ishlayapti (vazifa #{active.pk})."
    return ""


def _bts_refusal(model_admin: Any, request: HttpRequest, obj: None) -> str:
    return BTS_FAKE if integration_modes.fake_in_production("bts") else ""


class ProviderSyncActions(OpsActionsMixin):
    """The two provider actions, on the jobs list and on the provider-state list."""

    actions_list = ("billz_sync", "bts_poll")

    @ops_action(
        description="Billz sinxronlashni ishga tushirish",
        title="Billz sinxronlashni ishga tushirish",
        body=(
            "Oxirgi muvaffaqiyatli sinxronlashdan keyingi o‘zgarishlar (delta) Billz’dan olinadi: narxlar va "
            "qoldiqlar yangilanadi. Import allaqachon ishlayotgan bo‘lsa, yangisi boshlanmaydi."
        ),
        submit="Ishga tushirish",
        icon="sync",
        url_path="billz-sync",
        available=_billz_refusal,
    )
    def billz_sync(self, request: HttpRequest, obj: None, form: ConfirmForm) -> ActionResult:
        if integration_modes.fake_in_production("billz"):
            return failed(BILLZ_FAKE)
        job, created = services.start_job(
            enums.JobKind.BILLZ_IMPORT,
            {"mode": enums.BillzImportMode.DELTA},
            actor=request.user,
            request=request,
        )
        if not created:
            return refused(f"Billz importi allaqachon navbatda yoki ishlayapti (vazifa #{job.pk}).")
        return done(
            f"Billz sinxronlash navbatga qo‘yildi (vazifa #{job.pk}).",
            audit_action="ops.billz_sync_requested",
            entity_type="job",
            entity_id=job.pk,
            after={"kind": job.kind, "mode": enums.BillzImportMode.DELTA},
        )

    @ops_action(
        description="BTS holatini so‘rash",
        title="BTS holatini so‘rash",
        body=(
            "Uzoq vaqt yangilanmagan BTS jo‘natmalarining holati hozir so‘raladi (odatda har 10 daqiqada "
            "avtomatik bajariladi). So‘rov allaqachon ishlayotgan bo‘lsa, ikkinchisi boshlanmaydi."
        ),
        submit="So‘rash",
        icon="local_shipping",
        url_path="bts-poll",
        available=_bts_refusal,
    )
    def bts_poll(self, request: HttpRequest, obj: None, form: ConfirmForm) -> ActionResult:
        if integration_modes.fake_in_production("bts"):
            return failed(BTS_FAKE)
        if not cache.add(BTS_POLL_COOLDOWN_KEY, 1, timeout=BTS_POLL_COOLDOWN):
            return refused("BTS holati yaqinda so‘ralgan: ikki daqiqadan keyin qayta urinib ko‘ring.")
        try:
            enqueue_task(BTS_POLL_TASK)
        except Exception:
            cache.delete(BTS_POLL_COOLDOWN_KEY)
            raise
        return done(
            "BTS holat so‘rovi navbatga qo‘yildi.",
            audit_action="ops.bts_poll_requested",
            entity_type="integration",
            entity_id="bts",
        )


class JobAdmin(ProviderSyncActions, ReadOnlyAdmin):
    list_display = (
        "id",
        "kind",
        status_column("state", "Holat", JOB_STATE_VARIANTS),
        "created_at",
        "started_at",
        "finished_at",
        "created_by",
    )
    list_filter = (dropdown("kind"), dropdown("state"), date_range("created_at"))
    ordering = ("-created_at", "-id")
    autocomplete_fields = ("created_by",)


class IntegrationStateAdmin(ProviderSyncActions, ReadOnlyAdmin):
    list_display = ("provider", "last_sync_at", "last_success_at", "last_error_at", "consecutive_failures")
    ordering = ("provider",)


admin.site.register(Job, JobAdmin)
admin.site.register(IntegrationState, IntegrationStateAdmin)
