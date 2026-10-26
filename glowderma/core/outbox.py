"""At-least-once delivery: handlers must be idempotent at the provider boundary."""

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db import transaction

from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage

logger = logging.getLogger(__name__)

Handler = Callable[[OutboxMessage], None]

_handlers: dict[str, Handler] = {}
_dead_handlers: dict[str, Handler] = {}
# Attempts after which a failing message goes dead (``core.tasks.dispatch_outbox``).
MAX_ATTEMPTS = 10


class PermanentFailure(Exception):
    """Raised by a handler when no retry can succeed (the provider refused the request for good).

    The dispatcher dead-letters the message at once instead of retrying it up to :data:`MAX_ATTEMPTS`
    times: ``status = "dead"``, ``last_error`` = the exception text (a clear reason for staff), the
    ``outbox.dead`` audit row and the topic's :func:`on_dead` hook, exactly like a message that exhausted
    its attempts. Transient failures keep raising any other exception and are retried with backoff.
    """


def register(topic: str) -> Callable[[Handler], Handler]:
    """Register an at-least-once handler; it must be idempotent at the provider boundary."""

    def decorate(handler: Handler) -> Handler:
        _handlers[topic] = handler
        return handler

    return decorate


def on_dead(topic: str) -> Callable[[Handler], Handler]:
    """Register the dead-letter hook of ``topic``.

    It runs once, after the message was marked ``dead`` and audited, so the orchestrating module can record
    the business consequence (a shipment marked failed, a refund marked failed with ``dead_letter``). It must
    be idempotent and must not raise; failures are logged and never retried.
    """

    def decorate(handler: Handler) -> Handler:
        _dead_handlers[topic] = handler
        return handler

    return decorate


def notify_dead(message: OutboxMessage) -> None:
    handler = _dead_handlers.get(message.topic)
    if handler is None:
        return
    try:
        message.refresh_from_db()
        handler(message)
    except Exception:
        logger.exception("Dead-letter hook failed topic=%s id=%s", message.topic, message.pk)


def enqueue(topic: str, payload: Any, *, dedupe_key: str, delay: timedelta | None = None) -> OutboxMessage:
    from glowderma.core.tasks import dispatch_outbox

    message, _created = OutboxMessage.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "topic": topic,
            "payload": payload,
            "available_at": timeutils.now() + (delay or timedelta()),
        },
    )
    transaction.on_commit(dispatch_outbox.delay)
    return message


def deliver(message: OutboxMessage) -> None:
    _handlers[message.topic](message)
