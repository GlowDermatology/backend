"""Optional alert webhook (docs/ops/monitoring.md).

:func:`alert` queues one ``alert.send`` outbox message; its handler POSTs a compact JSON document
``{"event", "env", "at", "fields", "host"}`` to ``ALERT_WEBHOOK_URL``, signed with HMAC-SHA256 over the exact
body bytes (header ``X-GD-Signature: sha256=<hex>``, key ``ALERT_WEBHOOK_SECRET``).

* **Disabled** while ``ALERT_WEBHOOK_URL`` is empty: nothing is queued.
* **Deduplicated**: at most one message per ``event`` and ``scope`` per fixed window of
  ``ALERT_DEDUPE_MINUTES``: the window number is part of the outbox dedupe key. The scope is what makes two
  alerts different actions: the outbox topic for ``outbox.dead``, the order id for the per-order Billz events
  (``billz.sale_dead``, ``billz.refund_by_hand``, ``billz.sale_of_cancelled_order``,
  ``billz.sale_state_unclear``, ``billz.sale_total_differs``), else empty.
* **Never breaks the caller**: the insert runs in a savepoint and any failure is logged at WARNING and
  swallowed. An alert raised inside a transaction that later rolls back is rolled back with it; the ERROR log
  line every call site also writes stays the primary signal.
* **Ids and counts only** (an allow-list, :func:`scrub_fields`): a value leaves the server only when it is a
  bool, ``None``, an int of fewer than seven digits, a finite float below a million, or a short code
  (:data:`CODE_PATTERN`: letters, digits, ``_ . : -``, at most 64 characters, no run of seven or more digits
  even across separators); NaN and infinities become ``null``, every other value (an e-mail, a phone, a long
  number, a date, free text) becomes :data:`REDACTED`. Keys pass the same code rule (a non-conforming key is
  dropped and counted in :data:`DROPPED_KEYS_FIELD`); a key naming personal data or a credential
  (:data:`SENSITIVE_KEY_PARTS`, :data:`SENSITIVE_KEY_TOKENS`) keeps its name but its whole value is redacted.
  Callers pass ids, counts and short codes — never names, phone numbers, addresses, tokens or a provider's
  free text. The destination must be hosted in Uzbekistan or receive no personal data
  (docs/ops/compliance.md).
* **Delivery** (``alert.send``): one POST through httpx, redirects not followed, the URL never logged; a 5 s
  timeout per connect/read/write and an overall deadline of :data:`DEADLINE_SECONDS` (a receiver trickling
  bytes cannot hold the worker), far inside the outbox lease, so a slow receiver never makes the dispatcher
  hand the message to a second worker while the first is still posting. 2xx → done; a transport error, a
  missed deadline, 408, 429 or 5xx → retried by the outbox with backoff (dead after ``outbox.MAX_ATTEMPTS``);
  any other answer → :class:`~glowderma.core.outbox.PermanentFailure` (dead at once). A dead ``alert.send``
  raises no ``outbox.dead`` alert of its own, so a broken webhook cannot loop.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import socket
import threading
from collections.abc import Mapping
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction

from glowderma.core import outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.pii import clean_text

logger = logging.getLogger(__name__)

ALERT_TOPIC = "alert.send"
SIGNATURE_HEADER = "X-GD-Signature"
TIMEOUT_SECONDS = 5.0
# The whole POST, from connect to the last byte of the answer; well below the outbox lease (5 minutes).
DEADLINE_SECONDS = 15.0
MAX_ITEMS = 50
MAX_DEPTH = 3
REDACTED = "<redacted>"
# The only strings that leave the server: short codes (topics, check names, enum literals, case codes).
CODE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
# Seven or more digits, also when split by the separators a code allows ("90_123_45_67"): a phone number,
# a card or document number, or an id too long to be one of ours.
_LONG_DIGIT_RUN = re.compile(r"\d(?:[_.:-]*\d){6,}")
# Ints of this many digits or more are masked (a phone number as an int: 998901234567).
MAX_INT = 10**6
# A key containing one of these names personal data or a credential: its value never leaves the server.
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "auth",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "card",
    "msisdn",
    "phone",
    "mobile",
    "email",
    "name",
    "recipient",
    "contact",
    "address",
    "passport",
    "pinfl",
    "birth",
)
# Short names matched as a whole word of the key (``api_key``, ``client_ip``, ``pinCode``) so that
# ``shipment_id`` or ``company`` stay readable.
SENSITIVE_KEY_TOKENS: frozenset[str] = frozenset(
    {"key", "otp", "pin", "cvv", "cvc", "pan", "tel", "fio", "ip", "dob", "inn"}
)
# Set to the number of keys dropped from a mapping because they were not short codes.
DROPPED_KEYS_FIELD = "dropped_keys"
_KEY_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
RETRYABLE_STATUSES = frozenset({408, 429})


class AlertDeliveryError(RuntimeError):
    """The webhook could not take the alert now (transport error, 408, 429, 5xx): the outbox retries."""


def enabled() -> bool:
    """Whether alerts are queued at all (``ALERT_WEBHOOK_URL`` is set)."""
    return bool(str(getattr(settings, "ALERT_WEBHOOK_URL", "") or "").strip())


def is_code(value: str) -> bool:
    """Whether ``value`` is a short code that may leave the server (:data:`CODE_PATTERN`, no long digit
    run)."""
    return CODE_PATTERN.fullmatch(value) is not None and _LONG_DIGIT_RUN.search(value) is None


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    if any(part in lowered for part in SENSITIVE_KEY_PARTS):
        return True
    return any(word.lower() in SENSITIVE_KEY_TOKENS for word in _KEY_WORDS.findall(key))


def _scrub(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -MAX_INT < value < MAX_INT else REDACTED
    if isinstance(value, float):
        if not math.isfinite(value):
            return None  # NaN / infinity: no JSON (and no jsonb) can carry it
        return value if -MAX_INT < value < MAX_INT else REDACTED
    if isinstance(value, str):
        return value if is_code(value) else REDACTED
    if isinstance(value, Mapping | list | tuple):
        if depth >= MAX_DEPTH - 1:  # a container here would be nested MAX_DEPTH + 1 levels deep
            return REDACTED
        if isinstance(value, Mapping):
            return scrub_fields(value, _depth=depth + 1)
        return [_scrub(item, depth + 1) for item in value[:MAX_ITEMS]]
    return REDACTED  # dates, decimals, bytes, sets, objects: not on the allow-list


def scrub_fields(fields: Mapping[Any, Any] | None, *, _depth: int = 0) -> dict[str, Any]:
    """``fields`` made safe to leave the server (module doc): at most :data:`MAX_ITEMS` keys, keys that are
    not short codes dropped (counted in :data:`DROPPED_KEYS_FIELD`), sensitive keys redacted, every value
    through the allow-list, objects nested at most :data:`MAX_DEPTH` levels deep (``fields`` itself is the
    first)."""
    clean: dict[str, Any] = {}
    dropped = 0
    for raw_key, value in list((fields or {}).items())[:MAX_ITEMS]:
        if not isinstance(raw_key, str) or not is_code(raw_key) or raw_key == DROPPED_KEYS_FIELD:
            dropped += 1
            continue
        clean[raw_key] = REDACTED if _sensitive(raw_key) else _scrub(value, _depth)
    if dropped:
        clean[DROPPED_KEYS_FIELD] = dropped
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


def _post_within_deadline(url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    """``httpx.post`` in a daemon thread, given up after :data:`DEADLINE_SECONDS` (httpx's own timeouts
    bound each operation, not the request: a receiver sending one byte every few seconds would never time
    out). A request given up keeps its thread until httpx's per-operation timeouts end it; the worker never
    waits for it, and the outbox retries the alert."""
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def post() -> None:
        try:
            outcome["response"] = httpx.post(
                url, content=body, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread as is
            outcome["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=post, name="alert-send", daemon=True).start()
    if not finished.wait(DEADLINE_SECONDS):
        raise httpx.TimeoutException(f"no complete answer within {DEADLINE_SECONDS:g} s")
    if "error" in outcome:
        raise outcome["error"]
    response: httpx.Response = outcome["response"]
    return response


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
        response = _post_within_deadline(url, body, headers)
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
    "DEADLINE_SECONDS",
    "SIGNATURE_HEADER",
    "AlertDeliveryError",
    "alert",
    "document",
    "enabled",
    "scrub_fields",
    "send_alert",
    "signature",
]
