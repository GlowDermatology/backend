"""Passwords of the admin API: changing one's own, the e-mailed set-password links and their tokens."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.uzsource import N_

# The Uzbek texts of the password validators' codes (``AUTH_PASSWORD_VALIDATORS``).
PASSWORD_MESSAGES = {
    "password_too_short": N_("Kamida 12 ta belgi."),
    "password_too_common": N_("Juda oddiy parol."),
    "password_entirely_numeric": N_("Faqat raqamlardan iborat bo‘lmasin."),
    "password_too_similar": N_("Login bilan juda o‘xshash."),
}


def validated_new_password(value: str, user: Any) -> str:
    """``value`` when it passes ``AUTH_PASSWORD_VALIDATORS`` for ``user``, else the validators' codes with
    their Uzbek texts."""
    try:
        validate_password(value, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(
            [
                serializers.ErrorDetail(
                    PASSWORD_MESSAGES.get(error.code or "", str(error.message)), code=error.code or "invalid"
                )
                for error in exc.error_list
            ]
        ) from exc
    return value


class ChangePasswordSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_new_password(self, value: str) -> str:
        return validated_new_password(value, self.context["request"].user)


class ActivationSerializer(serializers.Serializer[dict[str, Any]]):
    """An e-mailed password link: queued for the person's address (``sent``), valid until ``expires_at``."""

    sent = serializers.BooleanField(help_text="The e-mail was queued (delivered by the outbox).")
    expires_at = serializers.DateTimeField()


class OwnPasswordLinkSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/password-link/`` (200): where the link went (masked) and until when it is valid."""

    email_masked = serializers.CharField()
    activation = ActivationSerializer()


class PasswordTokenSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/password-token/check/`` body."""

    token = serializers.CharField(max_length=128, trim_whitespace=False)


class PasswordTokenSetSerializer(PasswordTokenSerializer):
    """``POST auth/password-token/set/`` body (the password is validated for the link's account)."""

    new_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


class PasswordTokenInfoSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/password-token/check/`` (200): whose link it is, for the set-password page."""

    email_masked = serializers.CharField(help_text="The account's e-mail, masked (`a***@glowderma.uz`).")
    full_name = serializers.CharField(allow_blank=True)
    purpose = serializers.ChoiceField(choices=enums.StaffPasswordPurpose.values)
    expires_at = serializers.DateTimeField()


__all__ = [
    "PASSWORD_MESSAGES",
    "ActivationSerializer",
    "ChangePasswordSerializer",
    "OwnPasswordLinkSerializer",
    "PasswordTokenInfoSerializer",
    "PasswordTokenSerializer",
    "PasswordTokenSetSerializer",
    "validated_new_password",
]
