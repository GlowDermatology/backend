"""Canonical customer identifiers: the one stored form of a submitted phone number or e-mail address, and the
per-identifier ident the auth throttles and lockouts key on (customer contract §1.8). Look-alike spellings of
one phone or mailbox share one value, so no spelling escapes the per-identifier limits."""

from __future__ import annotations

import re
import unicodedata

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import EmailValidator

from glowderma.core.pii import normalize_email, pii_ref

ASCII_PHONE = re.compile(r"\+998[0-9]{9}")
# The bucket every unusable phone or e-mail spelling shares (fail closed: none escapes the limits).
INVALID_IDENTIFIER_BUCKET = "invalid"


def canonical_phone(value: object) -> str | None:
    """The ASCII E.164 form (``+998`` and nine ASCII digits) of a submitted phone, or ``None``.

    Every Unicode decimal digit (``str.isdecimal``: Arabic-Indic, fullwidth, …, exactly what a Unicode
    ``\\d`` matches) is folded to ASCII first, so look-alike spellings of one number share one canonical
    value and therefore one per-phone throttle key (customer contract §1.8: the per-phone rules are the main
    protection of the auth flows).
    """
    if not isinstance(value, str):
        return None
    folded = "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in value.strip())
    return folded if ASCII_PHONE.fullmatch(folded) else None


def phone_throttle_ident(value: object) -> str | None:
    """The per-phone throttle or guard ident of a submitted ``phone``: ``pii_ref`` of the canonical number;
    ``invalid`` for any other present value (fail closed: no spelling escapes the per-phone limits;
    :class:`SmsPhoneThrottle` scopes that bucket per device); ``None`` when the field is absent or blank (the
    view answers 400 ``required``). P1 keys the ``customer_login`` guard with it too."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    canonical = canonical_phone(value)
    return pii_ref(canonical) if canonical is not None else INVALID_IDENTIFIER_BUCKET


EMAIL_MAX_LENGTH = 254
_EMAIL_VALIDATOR = EmailValidator()


def canonical_email(value: object) -> str | None:
    """The stored form of a submitted e-mail address (``core.pii.normalize_email``: NFC, lower case, punycode
    domain; ≤254 characters, a valid address by Django's ``EmailValidator``), or ``None`` (hybrid identity:
    one account and one throttle key per mailbox however it is written)."""
    if not isinstance(value, str):
        return None
    folded = normalize_email(value)
    if not folded or len(folded) > EMAIL_MAX_LENGTH:
        return None
    try:
        _EMAIL_VALIDATOR(folded)
    except DjangoValidationError:
        return None
    return folded


def email_throttle_ident(value: object) -> str | None:
    """:func:`phone_throttle_ident` for an e-mail: ``pii_ref`` of the canonical address, ``invalid`` for any
    other present value, ``None`` when absent or blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    canonical = canonical_email(value)
    return pii_ref(canonical) if canonical is not None else INVALID_IDENTIFIER_BUCKET


def identifier_throttle_ident(data: object) -> str | None:
    """The per-identifier ident of an auth body: its ``phone`` when present, else its ``email``."""
    if not isinstance(data, dict):
        return None
    phone = phone_throttle_ident(data.get("phone"))
    return phone if phone is not None else email_throttle_ident(data.get("email"))


__all__ = [
    "ASCII_PHONE",
    "EMAIL_MAX_LENGTH",
    "INVALID_IDENTIFIER_BUCKET",
    "canonical_email",
    "canonical_phone",
    "email_throttle_ident",
    "identifier_throttle_ident",
    "phone_throttle_ident",
]
