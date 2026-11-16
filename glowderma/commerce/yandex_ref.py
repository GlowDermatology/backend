"""The order reference of a Yandex Delivery status callback (plan §3.7, decision 10).

Yandex signs nothing: a callback is an unauthenticated hint. Each claim registers its own callback URL
``YANDEX_DELIVERY_CALLBACK_URL + "?ref=<order_id>-<hmac>&"`` (Yandex appends ``updated_ts=…&claim_id=…`` by
concatenation); the view verifies the ``hmac`` part in constant time before it reads anything, so nobody can
make us look at someone else's order by guessing ids. ``hmac`` = the first 16 lower-case hex characters
of ``HMAC-SHA256(k, str(order_id))`` with ``k = HMAC-SHA256(PAYMENT_REF_KEY, "glowderma.yandex.callback.v1")``
— a key of its own derived from ``PAYMENT_REF_KEY`` (domain separation: a payment reference never verifies
as a callback reference, nor the reverse). Nothing is stored; the value is recomputed. Like the payment
reference it is a query string: the edge and gunicorn log no query string and ``WebhookInbox.path`` has none.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from django.conf import settings

REF_LENGTH = 16
DOMAIN = b"glowderma.yandex.callback.v1"
QUERY_KEY = "ref"
_PATTERN = re.compile(r"([1-9][0-9]{0,18})-([0-9a-f]{16})")


def _key() -> bytes:
    return hmac.new(str(settings.PAYMENT_REF_KEY).encode(), DOMAIN, hashlib.sha256).digest()


def _ref(order_id: int) -> str:
    return hmac.new(_key(), str(order_id).encode(), hashlib.sha256).hexdigest()[:REF_LENGTH]


def build(order_id: int) -> str:
    """``"10248-3f9a0c1b2d4e5f60"``."""
    return f"{order_id}-{_ref(order_id)}"


def callback_url(order_id: int) -> str | None:
    """The claim's ``callback_url`` (ending in ``&``), or ``None`` while ``YANDEX_DELIVERY_CALLBACK_URL`` is
    unset (the 60 s poll alone keeps the statuses current)."""
    base = str(getattr(settings, "YANDEX_DELIVERY_CALLBACK_URL", "") or "")
    if not base:
        return None
    return f"{base}?{QUERY_KEY}={build(order_id)}&"


def verify(value: object) -> int | None:
    """The order id of a well-formed reference whose HMAC part verifies, else ``None``."""
    if not isinstance(value, str):
        return None
    match = _PATTERN.fullmatch(value)
    if match is None:
        return None
    order_id = int(match.group(1))
    if not hmac.compare_digest(match.group(2), _ref(order_id)):
        return None
    return order_id


__all__ = ["DOMAIN", "QUERY_KEY", "REF_LENGTH", "build", "callback_url", "verify"]
