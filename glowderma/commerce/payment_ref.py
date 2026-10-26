"""The provider account reference ``"<order_id>-<ref>"`` (customer contract §1.4, §2.7.2).

``ref`` = the first 10 lower-case hex characters of ``HMAC-SHA256(PAYMENT_REF_KEY, str(order_id))``. Nothing
is
stored: the value is recomputed. Click Prepare, Payme ``CheckPerformTransaction`` and ``CreateTransaction``
:func:`verify` it with ``hmac.compare_digest`` before any order lookup, so nobody can open or block a payment
for
someone else's order by guessing sequential ids.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from django.conf import settings

REF_LENGTH = 10
_PATTERN = re.compile(r"([1-9][0-9]{0,18})-([0-9a-f]{10})")


def _ref(order_id: int) -> str:
    key = str(settings.PAYMENT_REF_KEY).encode()
    return hmac.new(key, str(order_id).encode(), hashlib.sha256).hexdigest()[:REF_LENGTH]


def build(order_id: int) -> str:
    """``"10248-3f9a0c1b2d"``."""
    return f"{order_id}-{_ref(order_id)}"


def verify(value: object) -> int | None:
    """The order id of a well-formed reference whose HMAC part verifies, else ``None`` (a bare order id, a
    malformed value and a wrong HMAC part all answer ``None``)."""
    if not isinstance(value, str):
        return None
    match = _PATTERN.fullmatch(value)
    if match is None:
        return None
    order_id = int(match.group(1))
    if not hmac.compare_digest(match.group(2), _ref(order_id)):
        return None
    return order_id
