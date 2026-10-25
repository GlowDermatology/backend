import logging
import uuid
from datetime import timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction
from django.db.models import Q

from glowderma.core import audit, outbox, timeutils
from glowderma.core.models import IdempotencyRecord, OutboxMessage

logger = logging.getLogger(__name__)


@shared_task(name="core.purge_idempotency_records")  # type: ignore[untyped-decorator]
def purge_idempotency_records() -> int:
    count, _ = IdempotencyRecord.objects.filter(expires_at__lte=timeutils.now()).delete()
    return count


def _claim_outbox_message() -> OutboxMessage | None:
    instant = timeutils.now()
    with transaction.atomic():
        message = (
            OutboxMessage.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status__in=["pending", "failed"]) | Q(status="in_flight", lease_expires_at__lte=instant),
                available_at__lte=instant,
            )
            .order_by("available_at", "id")
            .first()
        )
        if message is None:
            return None
        if message.status == "in_flight":
            message.attempts += 1
        message.status = "in_flight"
        message.claim_token = uuid.uuid4()
        message.lease_expires_at = instant + timedelta(minutes=5)
        message.save(update_fields=["status", "lease_expires_at", "claim_token", "attempts"])
        return message


def _complete_outbox_message(message: OutboxMessage, claim_token: uuid.UUID | None, **updates: Any) -> bool:
    updated = OutboxMessage.objects.filter(pk=message.pk, claim_token=claim_token, status="in_flight").update(
        **updates, updated_at=timeutils.now()
    )
    if not updated:
        logger.warning("Ignoring stale outbox completion id=%s claim_token=%s", message.pk, claim_token)
    return bool(updated)


@shared_task(name="core.dispatch_outbox")  # type: ignore[untyped-decorator]
def dispatch_outbox(batch: int = 50) -> None:
    for _ in range(batch):
        message = _claim_outbox_message()
        if message is None:
            break
        claim_token = message.claim_token
        attempts = message.attempts
        try:
            outbox.deliver(message)
        except Exception as exc:
            attempts += 1
            last_error = str(exc)
            available_at = timeutils.now() + timedelta(seconds=min(2**attempts, 3600))
            status = "dead" if attempts >= 10 else "failed"
            with transaction.atomic():
                completed = _complete_outbox_message(
                    message,
                    claim_token,
                    attempts=attempts,
                    last_error=last_error,
                    available_at=available_at,
                    status=status,
                    lease_expires_at=None,
                )
                if completed and status == "dead":
                    audit.record(
                        action="outbox.dead",
                        entity_type="outbox",
                        entity_id=str(message.pk),
                        after={
                            "topic": message.topic,
                            "dedupe_key": message.dedupe_key,
                            "attempts": attempts,
                            "last_error": last_error,
                        },
                    )
            if completed and status == "dead":
                outbox.notify_dead(message)
        else:
            _complete_outbox_message(message, claim_token, status="done", lease_expires_at=None)
