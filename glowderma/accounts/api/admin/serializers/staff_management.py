"""Managing staff accounts (manager only, admin contract §2.16): create, update, reset the password or the
MFA, and the one-time credentials answer."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.accounts.api.admin.serializers.fields import full_name_field, password_delivery_field
from glowderma.accounts.api.admin.serializers.passwords import ActivationSerializer
from glowderma.accounts.api.admin.serializers.staff_directory import StaffMemberSerializer
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error


class StaffCredentialsSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/`` (201) and ``POST staff/{id}/reset-password/`` (200): the account, its temporary
    password (shown once) and/or the e-mailed link."""

    staff = StaffMemberSerializer()
    temporary_password = serializers.CharField(
        allow_null=True,
        help_text="Shown once (`delivery` `temporary_password` or `both`, else `null`); the person signs in "
        "with it and must then set their own password. A replay of the same `Idempotency-Key` returns it "
        "again (stored sealed, §1.7).",
    )
    activation = ActivationSerializer(
        allow_null=True, help_text="The e-mailed link (`delivery` `email_link` or `both`), else `null`."
    )


class StaffLinkSentSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/{id}/send-password-link/`` (200)."""

    staff = StaffMemberSerializer()
    activation = ActivationSerializer()


class WorkforceInviteSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/`` body: ``email`` (trimmed, lower-cased, unique), ``full_name`` (trimmed 2..150),
    ``is_manager`` (default ``false``), ``delivery`` (default ``temporary_password``), ``language`` (the
    person's panel and e-mail language, default ``uz``)."""

    email = serializers.EmailField(max_length=254)
    full_name = full_name_field(required=True)
    delivery = password_delivery_field()
    language = serializers.ChoiceField(
        choices=enums.ContentLanguage.values, required=False, default=enums.ContentLanguage.UZ
    )

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class StaffCreateSerializer(WorkforceInviteSerializer):
    """Create a staff account, optionally granting the manager role."""

    is_manager = serializers.BooleanField(required=False, default=False)


class StaffResetPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/{id}/reset-password/`` body (optional): ``delivery`` (default ``temporary_password``)."""

    delivery = password_delivery_field()


class StaffResetMfaSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/{id}/reset-mfa/`` body (optional): ``reason`` (trimmed, up to 200 characters), kept on
    the ``auth.mfa_reset`` audit entry."""

    reason = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")


# Keys of a staff account that ``PATCH staff/{id}/`` refuses with ``read_only`` (the email is the sign-in
# identity; the state and the password have their own actions).
STAFF_READ_ONLY_KEYS = ("is_active", "password")


class StaffUpdateSerializer(serializers.Serializer[dict[str, Any]]):
    """``PATCH staff/{id}/`` body: ``full_name``, ``is_manager`` and/or ``email`` (the login address: the
    same account, open password links are revoked); ``is_active`` and ``password`` → ``read_only``."""

    full_name = full_name_field(required=False)
    is_manager = serializers.BooleanField(required=False)
    email = serializers.EmailField(max_length=254, required=False)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        body = data if isinstance(data, dict) else {}
        refused = {key: [field_error("read_only")] for key in STAFF_READ_ONLY_KEYS if key in body}
        try:
            value: dict[str, Any] = super().to_internal_value(data)
        except serializers.ValidationError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise serializers.ValidationError({**detail, **refused}) from exc
        if refused:
            raise serializers.ValidationError(refused)
        return value


__all__ = [
    "STAFF_READ_ONLY_KEYS",
    "StaffCreateSerializer",
    "StaffCredentialsSerializer",
    "StaffLinkSentSerializer",
    "StaffResetMfaSerializer",
    "StaffResetPasswordSerializer",
    "StaffUpdateSerializer",
    "WorkforceInviteSerializer",
]
