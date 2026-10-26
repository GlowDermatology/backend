"""The ``WebhookInbox`` writer of the provider callbacks (customer contract §2.13.0, T5).

Every delivery is stored with how it was answered, in the transaction of its effect, **last**: a first
delivery under its own ``event_key``, a replay of a key that already has a row under
``dup:{event_key}:{uuid4}``, an authentication/signature failure under ``rejected:{uuid4}`` as a bounded row
(allow-listed headers, the payload cut to 1 KiB of its JSON text, no raw body) and at most
``WEBHOOK_REJECTED_PER_IP_PER_MIN`` such rows per IP per minute (beyond that nothing is stored and one sampled
WARNING per IP per minute carries the count). Credentials (``Authorization``, tokens, the BTS secret) never
reach a stored row; ``path`` has no query string.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import time
import uuid
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder

from glowderma.core import timeutils
from glowderma.integrations.click import protocol as click_protocol
from glowderma.integrations.models import WebhookInbox

logger = logging.getLogger(__name__)

ALLOWED_HEADERS: tuple[str, ...] = ("Content-Type", "User-Agent", "X-Forwarded-For")
RAW_BODY_MAX = 64 * 1024
REJECTED_PAYLOAD_MAX = 1024
OUTCOME_ACCEPTED = "accepted"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"


def semantic_fingerprint(params: Any) -> str:
    """The Click duplicate check (§2.13.0): SHA-256 over the semantic fields, never ``sign_time``,
    ``sign_string``, ``error_note`` or ``click_paydoc_id``."""
    return click_protocol.fingerprint(params if isinstance(params, dict) else {})


def client_ip(request: Any) -> str | None:
    ip = getattr(request, "client_ip", None) or request.META.get("REMOTE_ADDR")
    return str(ip) if ip else None


def ip_allowed(request: Any, networks: Iterable[Any]) -> bool:
    """The optional provider IP allow-list (defence in depth only): empty → every IP."""
    allowed = list(networks)
    if not allowed:
        return True
    ip = client_ip(request)
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    return any(address in network for network in allowed)


def headers_of(request: Any) -> dict[str, str]:
    return {name: str(request.headers[name]) for name in ALLOWED_HEADERS if name in request.headers}


def _base(provider: str, request: Any, started: float) -> dict[str, Any]:
    return {
        "provider": provider,
        "remote_ip": client_ip(request),
        "request_id": str(getattr(request, "request_id", ""))[:64],
        "method": str(request.method)[:8],
        "path": str(request.path)[:200],  # never the query string (a BTS ``token=`` must not be stored)
        "headers": headers_of(request),
        "processing_ms": max(0, int((time.monotonic() - started) * 1000)),
        "received_at": timeutils.now(),
    }


def result(status: int, body: Any, outcome: str, error_code: str | None = None) -> dict[str, Any]:
    return {"status": status, "body": body, "outcome": outcome, "error_code": error_code}


EVENT_KEY_MAX = 120  # longer natural keys are hashed, so ``dup:{key}:{uuid4}`` always fits the 160 column


def normalise_key(event_key: str) -> str:
    """The stored form of a natural key: as is up to :data:`EVENT_KEY_MAX` characters, else
    ``sha256:<hex>`` of it (the same key always maps to the same stored key)."""
    if len(event_key) <= EVENT_KEY_MAX:
        return event_key
    return "sha256:" + hashlib.sha256(event_key.encode()).hexdigest()


def exists(provider: str, event_key: str) -> WebhookInbox | None:
    return WebhookInbox.objects.filter(provider=provider, event_key=normalise_key(event_key)).first()


def store_delivery(
    provider: str,
    event_key: str,
    request: Any,
    payload: Any,
    *,
    status: int,
    body: Any,
    outcome: str,
    error_code: str | None = None,
    entity_ref: str = "",
    raw_body: str = "",
    started: float,
) -> WebhookInbox:
    """An authenticated delivery: under ``event_key``, or ``dup:{event_key}:{uuid4}`` when the key already has
    a row (never a second row under one key); a duplicate keeps no raw body."""
    key = normalise_key(event_key)
    if exists(provider, key) is not None:
        key = f"dup:{key}:{uuid.uuid4().hex}"
        outcome = OUTCOME_DUPLICATE if outcome == OUTCOME_ACCEPTED else outcome
        raw_body = ""
    return WebhookInbox.objects.create(
        **_base(provider, request, started),
        event_key=key,
        payload=payload,
        raw_body=raw_body[:RAW_BODY_MAX],
        entity_ref=entity_ref[:64],
        result=result(status, body, outcome, error_code),
    )


def store_conflict(
    provider: str,
    request: Any,
    payload: Any,
    *,
    status: int,
    body: Any,
    error_code: str,
    started: float,
    raw_body: str = "",
) -> WebhookInbox:
    """An authenticated delivery refused before its state is read (a conflicting fingerprint; a Click
    action, service id or account reference that does not fit): a ``rejected:{uuid4}`` row, **uncapped**,
    never under the delivery's own key."""
    return WebhookInbox.objects.create(
        **_base(provider, request, started),
        event_key=f"rejected:{uuid.uuid4().hex}",
        payload=payload,
        raw_body=raw_body[:RAW_BODY_MAX],
        result=result(status, body, OUTCOME_REJECTED, error_code),
    )


def _bounded_payload(payload: Any) -> str:
    try:
        text = json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, sort_keys=True)
    except TypeError, ValueError:
        text = str(payload)
    return text[:REJECTED_PAYLOAD_MAX]


def store_rejected(
    provider: str, request: Any, payload: Any, *, status: int, body: Any, error_code: str, started: float
) -> WebhookInbox | None:
    """An authentication/signature failure (or a fake provider under the production settings): a bounded
    ``rejected:{uuid4}`` row, at most ``WEBHOOK_REJECTED_PER_IP_PER_MIN`` per IP per minute (``None`` when the
    cap is reached: nothing stored, one sampled WARNING per IP per minute)."""
    ip = client_ip(request) or "-"
    minute = int(timeutils.now().timestamp() // 60)
    counter = f"gd:webhook:rejected:{ip}:{minute}"
    cache.add(counter, 0, timeout=120)
    try:
        count = int(cache.incr(counter))
    except ValueError:
        cache.set(counter, 1, timeout=120)
        count = 1
    if count > int(settings.WEBHOOK_REJECTED_PER_IP_PER_MIN):
        if cache.add(f"gd:webhook:rejected:warned:{ip}:{minute}", 1, timeout=120):
            logger.warning("webhook.rejected_capped %s", {"provider": provider, "ip": ip, "count": count})
        return None
    return WebhookInbox.objects.create(
        **_base(provider, request, started),
        event_key=f"rejected:{uuid.uuid4().hex}",
        payload=_bounded_payload(payload),
        raw_body="",
        result=result(status, body, OUTCOME_REJECTED, error_code),
    )
