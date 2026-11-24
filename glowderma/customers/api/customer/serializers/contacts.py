"""``me/phone/*`` and ``me/email/*`` shapes: requesting a code and attaching the verified phone or e-mail."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.customers.api.customer.serializers.fields import (
    CustomerEmailField,
    CustomerPhoneField,
    clean_code,
    code_field,
    password_field,
)


class MePhoneCodeSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST me/phone/code/``."""

    phone = CustomerPhoneField()


class MeEmailCodeSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST me/email/code/``."""

    email = CustomerEmailField()


class _AttachSerializer(serializers.Serializer[dict[str, Any]]):
    code = code_field()
    password = password_field(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)

    def validate_code(self, value: str) -> str:
        return clean_code(value)


class MePhoneSerializer(_AttachSerializer):
    """``POST me/phone/``: the phone, its SMS code and, unless the session is fresh, the re-authentication."""

    phone = CustomerPhoneField()


class MeEmailSerializer(_AttachSerializer):
    """``POST me/email/``: the address, its e-mail code and, unless the session is fresh, the
    re-authentication."""

    email = CustomerEmailField()


__all__ = [
    "MeEmailCodeSerializer",
    "MeEmailSerializer",
    "MePhoneCodeSerializer",
    "MePhoneSerializer",
]
