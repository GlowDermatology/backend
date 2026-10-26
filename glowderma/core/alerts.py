"""Optional alert webhook (docs/ops/monitoring.md).

:func:`alert` queues one ``alert.send`` outbox message; its handler POSTs a compact JSON document
``{"event", "env", "at", "fields", "host"}`` to ``ALERT_WEBHOOK_URL``, signed with HMAC-SHA256 over the exact
body bytes (header ``X-GD-Signature: sha256=<hex>``, key ``ALERT_WEBHOOK_SECRET``).

* **Disabled** while ``ALERT_WEBHOOK_URL`` is empty: nothing is queued.
* **Deduplicated**: at most one message per ``event`` and ``scope`` (the outbox topic of ``outbox.dead``, else
  empty) per fixed window of ``ALERT_DEDUPE_MINUTES``: the window number is part of the outbox dedupe key.
* **Never breaks the caller**: the insert runs in a savepoint and any failure is logged at WARNING and
  swallowed. An alert raised inside a transaction that later rolls back is rolled back with it; the ERROR log
  line every call site also writes stays the primary signal.
* **Ids and counts only**: :func:`scrub_fields` redacts keys that name personal data or credentials and passes
  every string through ``core.pii.scrub_free_text`` (control characters dropped, runs of seven or more
  digits masked), cut to :data:`TEXT_LIMIT` characters. Callers pass ids, counts and short codes — never
  names, phone numbers, addresses, tokens or a provider's free text. The destination must be hosted in
  Uzbekistan or receive no personal data (docs/ops/compliance.md).
* **Delivery** (``alert.send``): one POST with a 5 s timeout through httpx, redirects not followed, the URL
  never logged. 2xx → done; a transport error, 408, 429 or 5xx → retried by the outbox with backoff (dead
  after ``outbox.MAX_ATTEMPTS``); any other answer → :class:`~glowderma.core.outbox.PermanentFailure` (dead
  at once). A dead ``alert.send`` raises no ``outbox.dead`` alert of its own, so a broken webhook cannot loop.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction

from glowderma.core import outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.pii import clean_text, scrub_free_text

logger = logging.getLogger(__name__)

ALERT_TOPIC = "alert.send"
SIGNATURE_HEADER = "X-GD-Signature"
TIMEOUT_SECONDS = 5.0
TEXT_LIMIT = 120
MAX_ITEMS = 50
MAX_DEPTH = 3
REDACTED = "<redacted>"
# A key containing one of these names personal data or a credential: its value never leaves the server.
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "phone",
    "name",
    "email",
    "address",
    "token",
    "secret",
    "password",
    "passwd",
    "card",
    "passport",
    "birth",
)
RETRYABLE_STATUSES = frozenset({408, 429})


class AlertDeliveryError(RuntimeError):
    """The webhook could not take the alert now (transport error, 408, 429, 5xx): the outbox retries."""


def enabled() -> bool:
    """Whether alerts are queued at all (``ALERT_WEBHOOK_URL`` is set)."""
    return bool(str(getattr(settings, "ALERT_WEBHOOK_URL", "") or "").strip())


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _scrub(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, str):
        return scrub_free_text(value, TEXT_LIMIT)
    if depth >= MAX_DEPTH - 1:  # a container here would be nested MAX_DEPTH + 1 levels deep
        return REDACTED
    if isinstance(value, Mapping):
        return scrub_fields(value, _depth=depth + 1)
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(item, depth + 1) for item in list(value)[:MAX_ITEMS]]
    return scrub_free_text(str(value), TEXT_LIMIT)


def scrub_fields(fields: Mapping[str, Any] | None, *, _depth: int = 0) -> dict[str, Any]:
    """``fields`` made safe to leave the server: at most :data:`MAX_ITEMS` keys, sensitive keys redacted,
    strings scrubbed and shortened, objects nested at most :data:`MAX_DEPTH` levels deep (``fields`` itself
    is the first)."""
    clean: dict[str, Any] = {}
    for raw_key, value in list((fields or {}).items())[:MAX_ITEMS]:
        key = clean_text(str(raw_key), 64)
        clean[key] = REDACTED if _sensitive(key) else _scrub(value, _depth)
    return clean


def _window_number() -> int:
    window = max(int(settings.ALERT_DEDUPE_MINUTES), 1) * 60
    return int(timeutils.now().timestamp()) // window


def alert(event: str, fields: Mapping[str, Any] | None = None, *, scope: str = "") -> bool:
    """Queue the alert ``event`` (ids and counts in ``fields``); ``True`` when a message was queued or already
    queued in this window, ``False`` when alerts are disabled or queueing failed. Never raises."""
    if not enabled():
        return False
    try:
        name = clean_text(event, 64)
        label = clean_text(scope, 100)
        payload = {
            "event": name,
            "at": timeutils.now().astimezone(timeutils.TASHKENT).isoformat(timespec="seconds"),
            "fields": scrub_fields(fields),
            "host": clean_text(socket.gethostname(), 64),
        }
        key = f"alert:{name}:{label}:{_window_number()}"[:254]
        with transaction.atomic():  # a savepoint inside the caller's transaction: a failure never poisons it
            # already queued in this window: no second message and no extra dispatch (a storm of the same
            # error — Redis down for every Billz cache call — costs one indexed read per call)
            if not OutboxMessage.objects.filter(dedupe_key=key).exists():
                outbox.enqueue(ALERT_TOPIC, payload, dedupe_key=key)
    except Exception as exc:  # noqa: BLE001 - an alert must never break the code that raised it
        logger.warning("alert.enqueue_failed event=%s error=%s", clean_text(event, 64), type(exc).__name__)
        return False
    return True


def document(message: OutboxMessage) -> bytes:
    """The exact body POSTed for ``message`` (compact JSON, sorted keys, ASCII)."""
    payload: dict[str, Any] = message.payload if isinstance(message.payload, dict) else {}
    body: dict[str, Any] = {
        "event": str(payload.get("event", "")),
        "env": str(settings.ALERT_ENV),
        "at": str(payload.get("at", "")),
        "fields": payload.get("fields") if isinstance(payload.get("fields"), dict) else {},
        "host": str(payload.get("host", "")),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def signature(body: bytes, secret: str) -> str:
    """``sha256=<hex HMAC-SHA256(secret, body)>`` — what the receiver recomputes over the raw body."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@outbox.register(ALERT_TOPIC)
def send_alert(message: OutboxMessage) -> None:
    """``alert.send``: POST the alert (module doc). Nothing is sent once the URL was removed."""
    url = str(getattr(settings, "ALERT_WEBHOOK_URL", "") or "").strip()
    payload = message.payload if isinstance(message.payload, dict) else {}
    event = clean_text(str(payload.get("event", "")), 64)
    if not url:
        logger.info("alert.send skipped event=%s: ALERT_WEBHOOK_URL is empty", event)
        return
    body = document(message)
    headers = {"Content-Type": "application/json", "User-Agent": "glowderma-alerts/1"}
    secret = str(getattr(settings, "ALERT_WEBHOOK_SECRET", "") or "")
    if secret:
        headers[SIGNATURE_HEADER] = signature(body, secret)
    try:
        response = httpx.post(
            url, content=body, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False
        )
    except httpx.HTTPError as exc:
        # the exception text may carry the URL (which can embed a token): only its type is kept
        logger.warning("alert.send_failed event=%s error=%s", event, type(exc).__name__)
        raise AlertDeliveryError(f"alert webhook unreachable ({type(exc).__name__})") from None
    status = response.status_code
    if 200 <= status < 300:
        logger.info("alert.sent event=%s status=%s", event, status)
        return
    logger.warning("alert.send_failed event=%s status=%s", event, status)
    if status in RETRYABLE_STATUSES or status >= 500:
        raise AlertDeliveryError(f"alert webhook answered HTTP {status}")
    raise outbox.PermanentFailure(f"alert webhook refused the alert: HTTP {status}")


__all__ = [
    "ALERT_TOPIC",
    "SIGNATURE_HEADER",
    "AlertDeliveryError",
    "alert",
    "document",
    "enabled",
    "scrub_fields",
    "send_alert",
    "signature",
]
