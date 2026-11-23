"""The admin panel's optional TOTP MFA (``auth/mfa/``): status, setup, confirm, disable, recovery codes and
the sign-in challenge."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.accounts.api.admin.serializers.fields import (
    RECOVERY_CODE_FIELD_LENGTH,
    mfa_code_field,
    password_field,
)
from glowderma.core.api.exceptions import field_error


class MfaChallengeSerializer(serializers.Serializer[dict[str, Any]]):
    """The 200 of ``auth/login/`` and ``auth/oauth/{provider}/`` for an account with the panel MFA on: no
    session yet, answer the challenge at ``auth/mfa/verify/``."""

    mfa_required = serializers.BooleanField(help_text="Always `true` (tells this answer from a session).")
    challenge = serializers.CharField(help_text="Opaque, single use; send it to `auth/mfa/verify/`.")
    challenge_expires_at = serializers.DateTimeField(help_text="5 minutes after the first step.")


class MfaStatusSerializer(serializers.Serializer[dict[str, Any]]):
    enabled = serializers.BooleanField(help_text="A confirmed admin panel TOTP device (= `me.mfa_enabled`).")
    recovery_codes_left = serializers.IntegerField(help_text="Unused recovery codes (0 while off).")


class MfaPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/setup/`` body."""

    current_password = password_field()


class MfaSetupSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/setup/`` (200): the pending device to scan (QR of ``otpauth_uri``) or type."""

    device_id = serializers.IntegerField()
    otpauth_uri = serializers.CharField(help_text='`otpauth://totp/…`, issuer "Glow Derma Admin".')
    manual_key = serializers.CharField(help_text="The secret in base32, in groups of four.")


class MfaConfirmSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/confirm/`` body."""

    device_id = serializers.IntegerField(min_value=1)
    code = mfa_code_field("The 6-digit code the authenticator shows now.")


class MfaRecoveryCodesSerializer(serializers.Serializer[dict[str, Any]]):
    """``auth/mfa/confirm/`` and ``auth/mfa/recovery-codes/`` (200): shown once, never again."""

    recovery_codes = serializers.ListField(
        child=serializers.CharField(), help_text="Ten `xxxxx-xxxxx` codes."
    )


class MfaDisableSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/disable/`` body."""

    current_password = password_field()
    code_or_recovery = serializers.CharField(
        max_length=RECOVERY_CODE_FIELD_LENGTH,
        write_only=True,
        help_text="The authenticator's 6-digit code or one unused recovery code.",
    )


class MfaRegenerateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/recovery-codes/`` body."""

    current_password = password_field()
    code = mfa_code_field("The 6-digit code the authenticator shows now.")


class MfaVerifySerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/verify/`` body: the challenge and exactly one of ``code`` / ``recovery_code``."""

    challenge = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    code = mfa_code_field("The authenticator's 6-digit code.", required=False)
    recovery_code = serializers.CharField(
        max_length=RECOVERY_CODE_FIELD_LENGTH,
        required=False,
        write_only=True,
        help_text="One unused recovery code instead of `code` (`xxxxx-xxxxx`; case and dashes are ignored).",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        code, recovery = attrs.get("code") or "", attrs.get("recovery_code") or ""
        if bool(code) == bool(recovery):
            raise serializers.ValidationError(
                {"code": [field_error("required" if not code else "invalid")]}, code="invalid"
            )
        return attrs


__all__ = [
    "MfaChallengeSerializer",
    "MfaConfirmSerializer",
    "MfaDisableSerializer",
    "MfaPasswordSerializer",
    "MfaRecoveryCodesSerializer",
    "MfaRegenerateSerializer",
    "MfaSetupSerializer",
    "MfaStatusSerializer",
    "MfaVerifySerializer",
]
