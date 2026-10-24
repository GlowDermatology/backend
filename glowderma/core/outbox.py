"""At-least-once delivery: handlers must be idempotent at the provider boundary."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db import transaction

from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage

_handlers: dict[str, Callable[[OutboxMessage], None]] = {}


def register(topic: str) -> Callable[[Callable[[OutboxMessage], None]], Callable[[OutboxMessage], None]]:
    """Register an at-least-once handler; it must be idempotent at the provider boundary."""

    def decorate(handler: Callable[[OutboxMessage], None]) -> Callable[[OutboxMessage], None]:
        _handlers[topic] = handler
        return handler

    return decorate


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
