"""The signed-in staff member's own profile (``auth/me/``) and capabilities."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.accounts.models import AuthSession
from glowderma.core import enums


class CapabilitiesSerializer(serializers.Serializer[dict[str, Any]]):
    staff_email_invite = serializers.BooleanField(
        help_text="Password links can be e-mailed (`delivery` `email_link` / `both`, "
        "`staff/{id}/send-password-link/`, `auth/password-link/`); `false` when the admin panel URL is not "
        "configured or e-mail is not live under the production settings: the UI shows the option disabled."
    )


class MeSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    display_name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.StaffRole.values)
    is_superuser = serializers.BooleanField()
    is_manager = serializers.BooleanField(
        help_text="May manage staff (`staff/` writes, §2.16); the UI shows the staff screen only then."
    )
    linked_providers = serializers.ListField(child=serializers.ChoiceField(choices=["google", "facebook"]))
    session_expires_at = serializers.DateTimeField()
    auth_method = serializers.ChoiceField(
        choices=AuthSession.AuthMethod.values,
        allow_null=True,
        help_text="How the current session's sign-in was proven: `password` (email + password) or the social "
        "provider (`google`, `facebook`); `null` for a session opened before the method was recorded. Only a "
        "`password` session may link a social identity (`auth/oauth/{provider}/link/`).",
    )
    must_change_password = serializers.BooleanField(
        help_text="The account must set its own password first: every admin route but `auth/me/`, "
        "`auth/change-password/`, `auth/password-link/` and `auth/logout/` answers 403 "
        "`password_change_required` until it does."
    )
    language = serializers.ChoiceField(
        choices=enums.ContentLanguage.values, help_text="The admin panel's language (`PATCH auth/me/`)."
    )
    mfa_enabled = serializers.BooleanField(help_text="The account has a confirmed admin panel TOTP device.")
    capabilities = CapabilitiesSerializer()


class MeUpdateSerializer(serializers.Serializer[dict[str, Any]]):
    """``PATCH auth/me/`` body: ``language`` (uz, ru, en)."""

    language = serializers.ChoiceField(choices=enums.ContentLanguage.values)


__all__ = [
    "CapabilitiesSerializer",
    "MeSerializer",
    "MeUpdateSerializer",
]
