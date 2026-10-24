from collections.abc import Iterable
from datetime import datetime
from typing import Any, TypeVar

from django.db import models

from glowderma.core import timeutils

ModelT = TypeVar("ModelT", bound=models.Model)


def timestamp_default() -> datetime:
    return timeutils.now()


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
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)
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
