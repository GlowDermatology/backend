"""Pseudonymous references for phones and device ids in logs and throttle keys (customer contract §6.4).

A plain SHA-256 of a ``+998`` number is brute-forced in seconds, so the reference is keyed:
``pii_ref(value)`` = the first 16 hex characters of ``HMAC-SHA256(LOG_PII_KEY, value)``.
"""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings


def pii_ref(value: str) -> str:
    key = str(settings.LOG_PII_KEY).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def mask_phone(phone: str) -> str:
    """``+998901234567`` → ``+998 90 *** ** 67`` (customer contract §1.4); anything else → ``***``."""
    if len(phone) == 13 and phone.startswith("+998") and phone[1:].isdigit():
        return f"+998 {phone[4:6]} *** ** {phone[-2:]}"
    return "***"


def mask_email(email: str) -> str:
    """``malika@gmail.com`` → ``m***@gmail.com`` (customer contract §1.4); no ``@`` → ``***``."""
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"
