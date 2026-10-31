"""Models of `integrations` — design 01 §2 (slice B2): webhook inbox, background jobs, provider state."""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

from glowderma.core import enums
from glowderma.core.models import AppendOnlyModel, TimeStampedModel, timestamp_default


class WebhookInbox(AppendOnlyModel):
    """Immutable record of every inbound provider call together with how it was answered.

    Inserted after processing, in the same transaction as the effect (01 §2.1); rows are never updated and are
    purged only inside ``core.db.purge_window()`` (trigger: ``integrations.0002``).
    """

    class Provider(models.TextChoices):  # internal
        CLICK = "click"
        PAYME = "payme"
        BTS = "bts"
        BILLZ = "billz"

    provider = models.CharField(max_length=16, choices=Provider.choices, verbose_name="provayder")
    # provider-specific natural key of the delivery
    event_key = models.CharField(max_length=160, verbose_name="hodisa kaliti")
    received_at = models.DateTimeField(default=timestamp_default, verbose_name="qabul qilingan")
    remote_ip = models.GenericIPAddressField(null=True, verbose_name="IP manzil")
    request_id = models.CharField(max_length=64, blank=True, verbose_name="so‘rov ID")
    method = models.CharField(max_length=8, verbose_name="usul")  # HTTP method
    path = models.CharField(max_length=200, verbose_name="yo‘l")
    # allow-listed headers only
    headers = models.JSONField(encoder=DjangoJSONEncoder, default=dict, verbose_name="HTTP sarlavhalari")
    # parsed body; null if unparsable
    payload = models.JSONField(encoder=DjangoJSONEncoder, null=True, verbose_name="ma’lumot")
    # verbatim body (≤64 KiB; longer bodies truncated + flagged)
    raw_body = models.TextField(blank=True, verbose_name="so‘rov tanasi")
    # "order:10248", "payment:301" — no FK upward
    entity_ref = models.CharField(max_length=64, blank=True, verbose_name="bog‘liq obyekt")
    # {"status", "body", "outcome", "error_code"}
    result = models.JSONField(encoder=DjangoJSONEncoder, verbose_name="natija")
    processing_ms = models.PositiveIntegerField(default=0, verbose_name="ishlov vaqti (ms)")

    class Meta:
        verbose_name = "webhook yozuvi"
        verbose_name_plural = "webhooklar"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_key"], name="webhookinbox_provider_event_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["provider", "received_at"], name="integr_webhook_provider_time"),
            models.Index(fields=["entity_ref"], name="integr_webhook_entity_ref"),
        ]
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        status = self.result.get("status") if isinstance(self.result, dict) else None
        return f"{self.provider} {self.event_key} → {status if status is not None else '?'}"


class Job(TimeStampedModel):
    """A background job the admin can start and poll (``billz_import`` in M1; the model is generic)."""

    kind = models.CharField(max_length=32, choices=enums.JobKind.choices, verbose_name="turi")
    state = models.CharField(
        max_length=16,
        choices=enums.JobState.choices,
        default=enums.JobState.QUEUED,
        db_index=True,
        verbose_name="holat",
    )
    # {"mode": "delta"|"full"}
    params = models.JSONField(encoder=DjangoJSONEncoder, default=dict, verbose_name="parametrlar")
    # {"done": 120, "total": 248} | null
    progress = models.JSONField(encoder=DjangoJSONEncoder, null=True, verbose_name="jarayon")
    # contract §2.14 result object
    result = models.JSONField(encoder=DjangoJSONEncoder, null=True, verbose_name="natija")
    # {"code": …, "message": …}
    error = models.JSONField(encoder=DjangoJSONEncoder, null=True, verbose_name="xato")
    started_at = models.DateTimeField(null=True, verbose_name="boshlangan")
    finished_at = models.DateTimeField(null=True, verbose_name="tugagan")
    # worker touches it every batch
    heartbeat_at = models.DateTimeField(null=True, verbose_name="oxirgi signal")
    # worker pickups (celery redelivery)
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="urinishlar")
    created_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="jobs",
        verbose_name="yaratgan",
    )
    celery_task_id = models.CharField(max_length=36, blank=True, verbose_name="Celery vazifa ID")

    class Meta:
        verbose_name = "fon vazifasi"
        verbose_name_plural = "fon vazifalari"
        constraints = [
            models.UniqueConstraint(
                fields=["kind"],
                condition=Q(state__in=[enums.JobState.QUEUED, enums.JobState.RUNNING]),
                name="job_single_flight_per_kind",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state=enums.JobState.QUEUED, started_at__isnull=True, finished_at__isnull=True)
                    | Q(state=enums.JobState.RUNNING, started_at__isnull=False, finished_at__isnull=True)
                    | Q(
                        state__in=[enums.JobState.SUCCEEDED, enums.JobState.FAILED],
                        started_at__isnull=False,
                        finished_at__isnull=False,
                    )
                ),
                name="job_state_timestamps_consistent",
            ),
            models.CheckConstraint(
                condition=~Q(state=enums.JobState.FAILED) | Q(error__isnull=False),
                name="job_failed_has_error",
            ),
        ]
        indexes = [models.Index(fields=["kind", "created_at"], name="integr_job_kind_created")]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.kind} #{self.pk} ({self.state})"


class IntegrationState(models.Model):
    """One row per provider with the last-sync facts the shell shows; never holds secrets."""

    class Provider(models.TextChoices):  # internal
        BILLZ = "billz"
        BTS = "bts"
        CLICK = "click"
        PAYME = "payme"
        ESKIZ = "eskiz"
        FCM = "fcm"

    provider = models.CharField(
        max_length=16, choices=Provider.choices, unique=True, verbose_name="provayder"
    )
    # last successful full/delta sync (billz) or poll (bts)
    last_sync_at = models.DateTimeField(null=True, verbose_name="oxirgi sinxronlash")
    # last successful call of any kind
    last_success_at = models.DateTimeField(null=True, verbose_name="oxirgi muvaffaqiyat")
    last_error_at = models.DateTimeField(null=True, verbose_name="oxirgi xato vaqti")
    # trimmed message of the last failure
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    consecutive_failures = models.PositiveIntegerField(default=0, verbose_name="ketma-ket xatolar")
    # provider cursors
    data = models.JSONField(encoder=DjangoJSONEncoder, default=dict, verbose_name="ma’lumotlar")
    updated_at = models.DateTimeField(default=timestamp_default, verbose_name="yangilangan")

    class Meta:
        verbose_name = "integratsiya holati"
        verbose_name_plural = "integratsiya holatlari"

    def __str__(self) -> str:
        return f"{self.provider}: synced {self.last_sync_at or 'never'}"
