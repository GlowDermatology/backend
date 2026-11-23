"""Field factories and lengths shared by the staff admin serializers."""

from __future__ import annotations

from rest_framework import serializers

from glowderma.core import enums

STAFF_FULL_NAME_MIN_LENGTH = 2


STAFF_FULL_NAME_MAX_LENGTH = 150


def full_name_field(*, required: bool) -> serializers.CharField:
    return serializers.CharField(
        required=required,
        min_length=STAFF_FULL_NAME_MIN_LENGTH,
        max_length=STAFF_FULL_NAME_MAX_LENGTH,
    )


def password_delivery_field() -> serializers.ChoiceField:
    return serializers.ChoiceField(
        choices=enums.StaffPasswordDelivery.values,
        required=False,
        default=enums.StaffPasswordDelivery.TEMPORARY_PASSWORD,
        help_text="`temporary_password` (shown once in the answer), `email_link` (a single-use link to the "
        "account's e-mail; no usable password until it is used) or `both`. The e-mail options need "
        "`Me.capabilities.staff_email_invite` (else `invalid_choice`).",
    )


MFA_CODE_FIELD_LENGTH = 16


RECOVERY_CODE_FIELD_LENGTH = 32


def password_field() -> serializers.CharField:
    return serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


def mfa_code_field(help_text: str, *, required: bool = True) -> serializers.CharField:
    return serializers.CharField(
        max_length=MFA_CODE_FIELD_LENGTH, required=required, write_only=True, help_text=help_text
    )


__all__ = [
    "MFA_CODE_FIELD_LENGTH",
    "RECOVERY_CODE_FIELD_LENGTH",
    "STAFF_FULL_NAME_MAX_LENGTH",
    "STAFF_FULL_NAME_MIN_LENGTH",
    "full_name_field",
    "mfa_code_field",
    "password_delivery_field",
    "password_field",
]
