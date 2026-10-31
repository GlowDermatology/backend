from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, TypeVar

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q

from glowderma.core import timeutils

ModelT = TypeVar("ModelT", bound=models.Model)


def timestamp_default() -> datetime:
    return timeutils.now()


def idempotency_expiry() -> datetime:
    return timeutils.now() + timedelta(hours=24)


class AppendOnlyError(RuntimeError):
    pass


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")
    updated_at = models.DateTimeField(default=timestamp_default, verbose_name="yangilangan")

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.updated_at = timeutils.now()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"updated_at"}
        super().save(*args, **kwargs)


class AppendOnlyQuerySet(models.QuerySet[ModelT]):
    def update(self, **kwargs: Any) -> int:
        raise AppendOnlyError("Append-only records cannot be updated")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise AppendOnlyError("Append-only records cannot be deleted")

    def bulk_update(
        self, objs: Iterable[ModelT], fields: Iterable[str], batch_size: int | None = None
    ) -> int:
        raise AppendOnlyError("Append-only records cannot be updated")


class AppendOnlyModel(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyError("Append-only records cannot be saved again")
        super().save(*args, **kwargs)

    def delete(self, using: str | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]:
        raise AppendOnlyError("Append-only records cannot be deleted")


class AuditEntry(AppendOnlyModel):
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")
    actor_type = models.CharField(
        max_length=16,
        choices=[(x, x) for x in ("staff", "customer", "system", "integration")],
        verbose_name="bajaruvchi turi",
    )
    actor_id = models.BigIntegerField(null=True)
    actor_label = models.CharField(max_length=254, blank=True, verbose_name="bajaruvchi")
    action = models.CharField(max_length=100, verbose_name="amal")
    entity_type = models.CharField(max_length=100, verbose_name="obyekt turi")
    entity_id = models.CharField(max_length=254, verbose_name="obyekt ID")
    before = models.JSONField(null=True, encoder=DjangoJSONEncoder, verbose_name="oldin")
    after = models.JSONField(null=True, encoder=DjangoJSONEncoder, verbose_name="keyin")
    reason = models.TextField(blank=True, verbose_name="sabab")
    ip = models.GenericIPAddressField(null=True, verbose_name="IP manzil")
    user_agent = models.TextField(blank=True, verbose_name="user-agent")
    request_id = models.CharField(max_length=64, blank=True, verbose_name="so‘rov ID")

    class Meta:
        verbose_name = "audit yozuvi"
        verbose_name_plural = "audit jurnali"
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["action"]),
        ]

    def __str__(self) -> str:
        return f"{self.action}: {self.entity_type}/{self.entity_id}"


class IdempotencyRecord(models.Model):
    audience = models.CharField(max_length=32, verbose_name="auditoriya")
    actor_id = models.BigIntegerField()
    method = models.CharField(max_length=10, verbose_name="usul")
    path = models.CharField(max_length=2048, verbose_name="yo‘l")
    key = models.UUIDField(verbose_name="kalit")
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(verbose_name="javob kodi")
    response_body = models.JSONField(encoder=DjangoJSONEncoder, null=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")
    expires_at = models.DateTimeField(
        default=idempotency_expiry, db_index=True, verbose_name="amal qilish muddati"
    )

    class Meta:
        verbose_name = "idempotentlik yozuvi"
        verbose_name_plural = "idempotentlik yozuvlari"
        constraints = [
            models.UniqueConstraint(
                fields=["audience", "actor_id", "method", "path", "key"], name="idempotency_scope_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.path}: {self.key}"


class OutboxMessage(TimeStampedModel):
    topic = models.CharField(max_length=100, verbose_name="topic")
    payload = models.JSONField(encoder=DjangoJSONEncoder, verbose_name="ma’lumot")
    dedupe_key = models.CharField(max_length=254, unique=True, verbose_name="takrorlanmaslik kaliti")
    status = models.CharField(
        max_length=16,
        default="pending",
        choices=[(value, value) for value in ("pending", "in_flight", "done", "failed", "dead")],
        verbose_name="holat",
    )
    attempts = models.PositiveIntegerField(default=0, verbose_name="urinishlar")
    available_at = models.DateTimeField(
        default=timestamp_default, db_index=True, verbose_name="navbatdagi urinish"
    )
    lease_expires_at = models.DateTimeField(null=True, verbose_name="ijara muddati")
    claim_token = models.UUIDField(null=True, editable=False, verbose_name="egallash tokeni")
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    # first ``outbox.RetryLater`` of this message (``give_up_after`` counts from here); null = never deferred
    deferred_since = models.DateTimeField(null=True, verbose_name="kechiktirilgan")

    class Meta:
        verbose_name = "outbox xabari"
        verbose_name_plural = "outbox xabarlari"
        indexes = [
            # every row that is not done (open and dead ones), in the dispatcher's claim order: the claim
            # (status pending/failed/in_flight, ORDER BY available_at, id LIMIT 1) reads it in order and stops
            # at the first due row, and the consistency report (K1 dead sales, K6) and requeue_outbox scan
            # only these few rows instead of the done ones that make up the table (core.0010)
            models.Index(
                fields=["available_at", "id"], condition=~Q(status="done"), name="outbox_unfinished_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.topic}: {self.dedupe_key} ({self.status})"
