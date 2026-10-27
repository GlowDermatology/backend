"""At-least-once delivery: handlers must be idempotent at the provider boundary.

A handler signals its outcome to ``core.tasks.dispatch_outbox`` by returning (``done``) or raising:

* :class:`PermanentFailure` — no retry can succeed: ``dead`` at once;
* :class:`RetryLater` — the provider cannot take the message *now* but will later (a closed Billz shift, a
  payment outcome that needs a quiet period): re-scheduled after ``delay`` **without** counting an attempt,
  dead with the reason once it has been deferred for longer than ``give_up_after``;
* anything else — a transient failure: retried with backoff, ``dead`` after :data:`MAX_ATTEMPTS`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import transaction

from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage

logger = logging.getLogger(__name__)

Handler = Callable[[OutboxMessage], None]

_handlers: dict[str, Handler] = {}
_dead_handlers: dict[str, Handler] = {}
_deferral_windows: dict[str, Callable[[], timedelta]] = {}
# Attempts after which a failing message goes dead (``core.tasks.dispatch_outbox``).
MAX_ATTEMPTS = 10


class PermanentFailure(Exception):
    """Raised by a handler when no retry can succeed (the provider refused the request for good).

    The dispatcher dead-letters the message at once instead of retrying it up to :data:`MAX_ATTEMPTS`
    times: ``status = "dead"``, ``last_error`` = the exception text (a clear reason for staff), the
    ``outbox.dead`` audit row and the topic's :func:`on_dead` hook, exactly like a message that exhausted
    its attempts. Transient failures keep raising any other exception and are retried with backoff.
    """


class RetryLater(Exception):
    """Raised by a handler when the provider refused *for now* (docs/integrations/billz.md, decision D2).

    The dispatcher sets ``available_at = now + delay`` and ``last_error = reason`` (status ``failed``) and
    leaves ``attempts`` alone, so a deferral never consumes the retry budget of :data:`MAX_ATTEMPTS`. The
    first deferral stamps ``OutboxMessage.deferred_since``; when ``give_up_after`` is set and the message has
    been deferred for that long, it goes ``dead`` like a :class:`PermanentFailure` (audit ``outbox.dead``, the
    topic's :func:`on_dead` hook) with ``reason`` and the deferral window in ``last_error``.
    """

    def __init__(self, delay: timedelta, reason: str, *, give_up_after: timedelta | None = None) -> None:
        if delay <= timedelta():
            raise ValueError("RetryLater needs a positive delay")
        super().__init__(reason)
        self.delay = delay
        self.reason = reason
        self.give_up_after = give_up_after


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


class RequeueRefused(Exception):
    """A :class:`RequeueHooks` callback refused (``manage.py requeue_outbox`` stops with its message)."""


@dataclass(frozen=True, slots=True)
class RequeueHooks:
    """How ``manage.py requeue_outbox`` treats the dead messages of one topic, registered by the module that
    owns the topic (``core`` knows no module above it):

    * ``describe(message)`` — what the message is about, appended to its listing line (e.g. the order and the
      provider's draft staff look up);
    * ``skip_reason(message)`` — non-empty: the message is listed but not requeued (its effect was already
      done another way, e.g. by hand);
    * ``resolve_refusal(message)`` — ``--resolved`` only; non-empty: the message is listed but not closed
      (closing it alone would leave the effect undone, e.g. a Billz sale not yet recorded by hand);
    * ``warning`` — printed once before the listing;
    * ``forget_payment_attempt(message, actor)`` — ``--forget-payment-attempt``: clears the topic's guard
      against a second payment once staff confirmed the provider never took the first one; audited by the
      callback; returns a line to print; raises :class:`RequeueRefused` to stop.
    """

    describe: Callable[[OutboxMessage], str]
    skip_reason: Callable[[OutboxMessage], str]
    warning: str = ""
    forget_payment_attempt: Callable[[OutboxMessage, Any], str] | None = None
    resolve_refusal: Callable[[OutboxMessage], str] | None = None


_requeue_hooks: dict[str, RequeueHooks] = {}


def on_requeue(topic: str, hooks: RequeueHooks) -> None:
    """Register the :class:`RequeueHooks` of ``topic``."""
    _requeue_hooks[topic] = hooks


def requeue_hooks(topic: str) -> RequeueHooks | None:
    return _requeue_hooks.get(topic)


def register_deferral_window(topic: str, window: Callable[[], timedelta]) -> None:
    """Declare how long a message of ``topic`` may stay deferred (:class:`RetryLater`) before it counts as
    stuck — the ``give_up_after`` its handler passes. The nightly consistency report (K6) reads it; topics
    that declare nothing get ``settings.OUTBOX_DEFERRAL_WINDOW_HOURS``."""
    _deferral_windows[topic] = window


def deferral_windows() -> dict[str, timedelta]:
    """``{topic: window}`` of every topic that declared one (:func:`register_deferral_window`)."""
    return {topic: window() for topic, window in _deferral_windows.items()}


def notify_dead(message: OutboxMessage) -> None:
    """Called once after ``message`` went dead and was audited: the ``outbox.dead`` alert (every topic but the
    alert topic itself, so a broken alert webhook cannot loop) and the topic's :func:`on_dead` hook."""
    from glowderma.core import alerts

    try:
        message.refresh_from_db()
    except Exception:
        logger.exception("Dead-letter hook failed topic=%s id=%s", message.topic, message.pk)
        return
    if message.topic != alerts.ALERT_TOPIC:
        alerts.alert(
            "outbox.dead",
            {"topic": message.topic, "message_id": message.pk, "attempts": message.attempts},
            scope=message.topic,
        )
    handler = _dead_handlers.get(message.topic)
    if handler is None:
        return
    try:
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
    # robust: the message already committed, so a broker outage is logged, never turned into a 500 for the
    # request that enqueued it; the beat (core.dispatch_outbox every 10 s) delivers it anyway.
    transaction.on_commit(dispatch_outbox.delay, robust=True)
    return message


def deliver(message: OutboxMessage) -> None:
    _handlers[message.topic](message)
