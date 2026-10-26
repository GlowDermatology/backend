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
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    updated_at = models.DateTimeField(default=timestamp_default)

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
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    actor_type = models.CharField(
        max_length=16, choices=[(x, x) for x in ("staff", "customer", "system", "integration")]
    )
    actor_id = models.BigIntegerField(null=True)
    actor_label = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100)
    entity_id = models.CharField(max_length=254)
    before = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(null=True, encoder=DjangoJSONEncoder)
    reason = models.TextField(blank=True)
    ip = models.GenericIPAddressField(null=True)
    user_agent = models.TextField(blank=True)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["action"]),
        ]

    def __str__(self) -> str:
        return f"{self.action}: {self.entity_type}/{self.entity_id}"


class IdempotencyRecord(models.Model):
    audience = models.CharField(max_length=32)
    actor_id = models.BigIntegerField()
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=2048)
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField(encoder=DjangoJSONEncoder, null=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    expires_at = models.DateTimeField(default=idempotency_expiry, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["audience", "actor_id", "method", "path", "key"], name="idempotency_scope_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.path}: {self.key}"


class OutboxMessage(TimeStampedModel):
    topic = models.CharField(max_length=100)
    payload = models.JSONField(encoder=DjangoJSONEncoder)
    dedupe_key = models.CharField(max_length=254, unique=True)
    status = models.CharField(
        max_length=16,
        default="pending",
        choices=[(value, value) for value in ("pending", "in_flight", "done", "failed", "dead")],
    )
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timestamp_default, db_index=True)
    lease_expires_at = models.DateTimeField(null=True)
    claim_token = models.UUIDField(null=True, editable=False)
    last_error = models.TextField(blank=True)
    # first ``outbox.RetryLater`` of this message (``give_up_after`` counts from here); null = never deferred
    deferred_since = models.DateTimeField(null=True)

    class Meta:
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
