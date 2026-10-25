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

    provider = models.CharField(max_length=16, choices=Provider.choices)
    event_key = models.CharField(max_length=160)  # provider-specific natural key of the delivery
    received_at = models.DateTimeField(default=timestamp_default)
    remote_ip = models.GenericIPAddressField(null=True)
    request_id = models.CharField(max_length=64, blank=True)
    method = models.CharField(max_length=8)  # HTTP method
    path = models.CharField(max_length=200)
    headers = models.JSONField(encoder=DjangoJSONEncoder, default=dict)  # allow-listed headers only
    payload = models.JSONField(encoder=DjangoJSONEncoder, null=True)  # parsed body; null if unparsable
    raw_body = models.TextField(blank=True)  # verbatim body (≤64 KiB; longer bodies truncated + flagged)
    entity_ref = models.CharField(max_length=64, blank=True)  # "order:10248", "payment:301" — no FK upward
    result = models.JSONField(encoder=DjangoJSONEncoder)  # {"status", "body", "outcome", "error_code"}
    processing_ms = models.PositiveIntegerField(default=0)

    class Meta:
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

    kind = models.CharField(max_length=32, choices=enums.JobKind.choices)
    state = models.CharField(
        max_length=16, choices=enums.JobState.choices, default=enums.JobState.QUEUED, db_index=True
    )
    params = models.JSONField(encoder=DjangoJSONEncoder, default=dict)  # {"mode": "delta"|"full"}
    progress = models.JSONField(encoder=DjangoJSONEncoder, null=True)  # {"done": 120, "total": 248} | null
    result = models.JSONField(encoder=DjangoJSONEncoder, null=True)  # contract §2.14 result object
    error = models.JSONField(encoder=DjangoJSONEncoder, null=True)  # {"code": …, "message": …}
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)  # worker touches it every batch
    attempts = models.PositiveSmallIntegerField(default=0)  # worker pickups (celery redelivery)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="jobs"
    )
    celery_task_id = models.CharField(max_length=36, blank=True)

    class Meta:
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

    provider = models.CharField(max_length=16, choices=Provider.choices, unique=True)
    last_sync_at = models.DateTimeField(null=True)  # last successful full/delta sync (billz) or poll (bts)
    last_success_at = models.DateTimeField(null=True)  # last successful call of any kind
    last_error_at = models.DateTimeField(null=True)
    last_error = models.TextField(blank=True)  # trimmed message of the last failure
    consecutive_failures = models.PositiveIntegerField(default=0)
    data = models.JSONField(encoder=DjangoJSONEncoder, default=dict)  # provider cursors
    updated_at = models.DateTimeField(default=timestamp_default)

    class Meta:
        verbose_name = "integration state"

    def __str__(self) -> str:
        return f"{self.provider}: synced {self.last_sync_at or 'never'}"
