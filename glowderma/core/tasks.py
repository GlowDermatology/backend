from datetime import timedelta

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction
from django.db.models import Q

from glowderma.core import audit, outbox, timeutils
from glowderma.core.models import IdempotencyRecord, OutboxMessage


@shared_task(name="core.purge_idempotency_records")  # type: ignore[untyped-decorator]
def purge_idempotency_records() -> int:
    count, _ = IdempotencyRecord.objects.filter(expires_at__lte=timeutils.now()).delete()
    return count


@shared_task(name="core.dispatch_outbox")  # type: ignore[untyped-decorator]
def dispatch_outbox(batch: int = 50) -> None:
    instant = timeutils.now()
    with transaction.atomic():
        messages = list(
            OutboxMessage.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status__in=["pending", "failed"]) | Q(status="in_flight", lease_expires_at__lte=instant),
                available_at__lte=instant,
            )
            .order_by("available_at", "id")[:batch]
        )
        for message in messages:
            if message.status == "in_flight":
                message.attempts += 1
            message.status = "in_flight"
            message.lease_expires_at = instant + timedelta(minutes=5)
            message.save(update_fields=["status", "lease_expires_at", "attempts"])
    for message in messages:
        try:
            outbox.deliver(message)
        except Exception as exc:
            message.attempts += 1
            message.last_error = str(exc)
            message.available_at = timeutils.now() + timedelta(seconds=min(2**message.attempts, 3600))
            message.status = "dead" if message.attempts >= 10 else "failed"
            message.lease_expires_at = None
            with transaction.atomic():
                message.save(
                    update_fields=["attempts", "last_error", "available_at", "status", "lease_expires_at"]
                )
                if message.status == "dead":
                    audit.record(
                        action="outbox.dead",
                        entity_type="outbox",
                        entity_id=str(message.pk),
                        after={
                            "topic": message.topic,
                            "dedupe_key": message.dedupe_key,
                            "attempts": message.attempts,
                            "last_error": message.last_error,
                        },
                    )
        else:
            message.status = "done"
            message.lease_expires_at = None
            message.save(update_fields=["status", "lease_expires_at"])
