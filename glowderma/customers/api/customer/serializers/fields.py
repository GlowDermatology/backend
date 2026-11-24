"""Field factories and cleaners shared by the customer serializers: phone, e-mail, password, name and the OTP
code."""

from __future__ import annotations

import re
from typing import Any

from rest_framework import serializers

from glowderma.accounts.security.identifiers import EMAIL_MAX_LENGTH, canonical_email, canonical_phone
from glowderma.core.api.exceptions import field_error

CODE_PATTERN = re.compile(r"^[0-9]{6}$")


class CustomerPhoneField(serializers.CharField):
    """E.164 ``+998`` + nine digits (Unicode digits folded to ASCII); else ``invalid``
    (``field.phone_invalid``)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", 32)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        phone = canonical_phone(value)
        if phone is None:
            raise serializers.ValidationError([field_error("invalid", message_key="field.phone_invalid")])
        return phone


class CustomerEmailField(serializers.CharField):
    """An e-mail address, returned normalised (trimmed, lower case); else ``invalid``
    (``field.email_invalid``, hybrid identity)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", EMAIL_MAX_LENGTH + 16)  # surrounding spaces are trimmed first
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        email = canonical_email(value)
        if email is None:
            raise serializers.ValidationError([field_error("invalid", message_key="field.email_invalid")])
        return email


def password_field(**kwargs: Any) -> serializers.CharField:
    return serializers.CharField(max_length=128, trim_whitespace=False, write_only=True, **kwargs)


def name_field(*, required: bool = True) -> serializers.CharField:
    # allow_blank: an empty or whitespace-only name reaches clean_name and answers the contract's
    # ``min_length`` (``field.name_min``), not DRF's ``blank``; a missing key still answers ``required``.
    return serializers.CharField(max_length=150, required=required, allow_null=not required, allow_blank=True)


def clean_name(value: str | None) -> str | None:
    """Names are trimmed and need at least 2 characters (C§2.1.3, §2.1.9, §2.2.2: ``min_length``
    ``field.name_min``, also for ``""`` and whitespace only)."""
    if value is None:
        return None
    value = value.strip()
    if len(value) < 2:
        raise serializers.ValidationError([field_error("min_length", message_key="field.name_min", limit=2)])
    return value


def code_field() -> serializers.CharField:
    return serializers.CharField(max_length=32)


def clean_code(value: str) -> str:
    if not CODE_PATTERN.fullmatch(value):
        raise serializers.ValidationError([field_error("invalid", message_key="field.otp_format")])
    return value


__all__ = [
    "CODE_PATTERN",
    "CustomerEmailField",
    "CustomerPhoneField",
    "clean_code",
    "clean_name",
    "code_field",
    "name_field",
    "password_field",
]
