"""Pseudonymous references for phones and device ids in logs and throttle keys (customer contract §6.4).

A plain SHA-256 of a ``+998`` number is brute-forced in seconds, so the reference is keyed:
``pii_ref(value)`` = the first 16 hex characters of ``HMAC-SHA256(LOG_PII_KEY, value)``.

Free text from a provider (an error message) goes through :func:`scrub_free_text` before it reaches a log
line, an outbox ``last_error`` or an audit row; :func:`clean_text` alone makes any text storable.
"""

from __future__ import annotations

import hashlib
import hmac
import re

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


# Control characters (C0 except tab/newline/carriage return, DEL, C1) and lone surrogates: PostgreSQL text
# refuses NUL and no UTF-8 column can store a lone surrogate, so an error text carrying one would make the
# very write that records the failure fail (an outbox message stuck ``in_flight`` for ever).
_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_WHITESPACE_RUN = re.compile(r"\s+")
# A run of at least seven digits, optionally separated by single spaces, dashes, dots or brackets, with an
# optional leading "+": a phone number (or a card / document number) in someone else's free text.
_DIGIT_RUN = re.compile(r"\+?\d(?:[ \-.()]{0,2}\d){6,}")
MASKED_NUMBER = "<number>"


def clean_text(value: str, limit: int | None = None) -> str:
    """``value`` without control characters or lone surrogates, whitespace collapsed, at most ``limit``
    characters: safe to store in a text column, an audit document or a log line."""
    text = _WHITESPACE_RUN.sub(" ", _UNSAFE_CHARACTERS.sub("", value)).strip()
    return text if limit is None else text[:limit]


def scrub_free_text(value: str, limit: int | None = None) -> str:
    """A provider's free-text message made safe to keep: :func:`clean_text` plus every run of seven or more
    digits (a phone number the provider echoed) replaced by :data:`MASKED_NUMBER`."""
    return clean_text(_DIGIT_RUN.sub(MASKED_NUMBER, clean_text(value)), limit)
