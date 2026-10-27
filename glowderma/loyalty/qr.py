"""The personal loyalty QR (customer contract §2.9.3) and its single-use resolve by a cashier (§2.10).

A token is ``"GD1:" + base64url(16 random bytes)`` (26 characters, 128 bits from ``secrets.token_bytes``). The
raw token is never stored server-side and never logged: ``core.qr_store`` keeps
``gd:loyalty:qr:{hex(HMAC-SHA256(QR_HMAC_KEY, token))}`` → ``"{customer_id}:{issued_at_epoch}"`` for
``QR_TOKEN_TTL_SECONDS`` (60) with at most ``QR_MAX_LIVE_TOKENS`` (2) live keys per customer, and the resolve
reads it with ``GETDEL`` — the only read — so two scans of one token race in Redis and exactly one wins.
No database write and no audit on issue (one call per minute per open screen).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings

from glowderma.core import qr_store, timeutils

TOKEN_PREFIX = "GD1:"  # noqa: S105 - the QR payload prefix, not a secret
TOKEN_PATTERN = re.compile(r"^GD1:[A-Za-z0-9_-]{22}$")
TOKEN_BYTES = 16
REFRESH_MARGIN_SECONDS = 10  # the app refreshes every 50 s for a 60 s token
CUSTOMER_REF_PREFIX = "c_"


@dataclass(frozen=True, slots=True)
class QrToken:
    token: str
    expires_at: datetime
    ttl_seconds: int
    refresh_in_seconds: int


def _hmac(message: str) -> str:
    key = str(settings.QR_HMAC_KEY).encode("utf-8")
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def key_hash(token: str) -> str:
    """The store key of a token: ``hex(HMAC-SHA256(QR_HMAC_KEY, token))``."""
    return _hmac(token)


def new_token() -> str:
    raw = base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")
    return TOKEN_PREFIX + raw


def is_well_formed(token: object) -> bool:
    return isinstance(token, str) and TOKEN_PATTERN.fullmatch(token) is not None


def ttl_seconds() -> int:
    return int(settings.QR_TOKEN_TTL_SECONDS)


def issue_qr(customer_id: int) -> QrToken:
    """A fresh single-use token for the customer (``SET NX EX``; issuing beyond the live limit drops the
    oldest key)."""
    ttl = ttl_seconds()
    max_live = int(settings.QR_MAX_LIVE_TOKENS)
    while True:
        token = new_token()
        if qr_store.put(key_hash(token), customer_id, ttl, max_live):
            break
    return QrToken(
        token=token,
        expires_at=timeutils.now() + timedelta(seconds=ttl),
        ttl_seconds=ttl,
        refresh_in_seconds=max(1, ttl - REFRESH_MARGIN_SECONDS),
    )


def resolve_qr(token: str) -> int | None:
    """The customer id behind a token, consuming it (``GETDEL``); ``None`` for a malformed, unknown, expired
    or already used token. Whether the customer is still active is the caller's check."""
    if not is_well_formed(token):
        return None
    value = qr_store.take(key_hash(token))
    if value is None:
        return None
    customer_part, _sep, _issued = value.partition(":")
    try:
        return int(customer_part)
    except ValueError:
        return None


def pos_customer_ref(device_id: int, customer_id: int) -> str:
    """Opaque and stable per (POS device, customer): ``"c_" + 16 hex of HMAC-SHA256(QR_HMAC_KEY,
    "pos:{device_id}:{customer_id}")`` — lets the cashier software recognise a repeat scan without learning
    our ids (C§2.10)."""
    return CUSTOMER_REF_PREFIX + _hmac(f"pos:{device_id}:{customer_id}")[:16]
