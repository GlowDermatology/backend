import logging
import uuid
from datetime import timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction
from django.db.models import Q

from glowderma.core import audit, outbox, timeutils
from glowderma.core.models import IdempotencyRecord, OutboxMessage
from glowderma.core.pii import clean_text

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


# ``last_error`` keeps this much of a failure's text (the column is unbounded; a reason fits easily).
LAST_ERROR_LIMIT = 2000


def _bury(message: OutboxMessage, claim_token: uuid.UUID | None, attempts: int, last_error: str) -> bool:
    """Mark the claimed message ``dead`` and audit it; returns whether this worker still owned it."""
    with transaction.atomic():
        completed = _complete_outbox_message(
            message,
            claim_token,
            attempts=attempts,
            last_error=last_error,
            status="dead",
            lease_expires_at=None,
        )
        if completed:
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
    return completed


def _defer(message: OutboxMessage, claim_token: uuid.UUID | None, exc: outbox.RetryLater) -> None:
    """``RetryLater``: re-schedule without counting an attempt, or bury once deferred for too long."""
    instant = timeutils.now()
    since = message.deferred_since or instant
    reason = clean_text(exc.reason, LAST_ERROR_LIMIT)
    if exc.give_up_after is not None and instant - since >= exc.give_up_after:
        last_error = clean_text(
            f"{reason} (deferred since {since.isoformat()}, "
            f"gave up after {exc.give_up_after.total_seconds() / 3600:g} h)",
            LAST_ERROR_LIMIT,
        )
        logger.error(
            "Outbox message dead after its deferral window topic=%s id=%s", message.topic, message.pk
        )
        if _bury(message, claim_token, message.attempts + 1, last_error):
            outbox.notify_dead(message)
        return
    logger.info(
        "Outbox message deferred topic=%s id=%s for %s: %s", message.topic, message.pk, exc.delay, reason
    )
    _complete_outbox_message(
        message,
        claim_token,
        status="failed",
        last_error=reason,
        available_at=instant + exc.delay,
        deferred_since=since,
        lease_expires_at=None,
    )


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
        except outbox.RetryLater as exc:
            _defer(message, claim_token, exc)
        except Exception as exc:
            attempts += 1
            # a provider's text may carry NUL or a lone surrogate, which the UPDATE below could not store: the
            # message would then stay in_flight and re-run at every lease expiry instead of failing
            last_error = clean_text(str(exc), LAST_ERROR_LIMIT) or type(exc).__name__
            available_at = timeutils.now() + timedelta(seconds=min(2**attempts, 3600))
            permanent = isinstance(exc, outbox.PermanentFailure)
            if permanent or attempts >= outbox.MAX_ATTEMPTS:
                if permanent:
                    logger.error(
                        "Outbox message dead on a permanent failure topic=%s id=%s", message.topic, message.pk
                    )
                if _bury(message, claim_token, attempts, last_error):
                    outbox.notify_dead(message)
                continue
            _complete_outbox_message(
                message,
                claim_token,
                attempts=attempts,
                last_error=last_error,
                available_at=available_at,
                status="failed",
                lease_expires_at=None,
            )
        else:
            _complete_outbox_message(message, claim_token, status="done", lease_expires_at=None)
