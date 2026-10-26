from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from glowderma.accounts.models import AuthSession
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import QueryBooleanField
from glowderma.core.api.serializers import ActorSerializer


class LoginSerializer(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class RefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh = serializers.CharField(trim_whitespace=False)


class LogoutSerializer(RefreshSerializer):
    scope = serializers.ChoiceField(choices=["current", "others", "all"], default="current")


class ChangePasswordSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_new_password(self, value: str) -> str:
        user = self.context["request"].user
        try:
            validate_password(value, user)
        except DjangoValidationError as exc:
            messages = {
                "password_too_short": "Kamida 12 ta belgi.",
                "password_too_common": "Juda oddiy parol.",
                "password_entirely_numeric": "Faqat raqamlardan iborat bo‘lmasin.",
                "password_too_similar": "Login bilan juda o‘xshash.",
            }
            raise serializers.ValidationError(
                [
                    serializers.ErrorDetail(
                        messages.get(error.code or "", str(error.message)), code=error.code or "invalid"
                    )
                    for error in exc.error_list
                ]
            ) from exc
        return value


class MeSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    display_name = serializers.CharField()
    is_superuser = serializers.BooleanField()
    linked_providers = serializers.ListField(child=serializers.ChoiceField(choices=["google", "facebook"]))
    session_expires_at = serializers.DateTimeField()
    auth_method = serializers.ChoiceField(
        choices=AuthSession.AuthMethod.values,
        allow_null=True,
        help_text="How the current session's sign-in was proven: `password` (email + password) or the social "
        "provider (`google`, `facebook`); `null` for a session opened before the method was recorded. Only a "
        "`password` session may link a social identity (`auth/oauth/{provider}/link/`).",
    )


class AccessSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh_expires_at = serializers.DateTimeField()


class LoginResponseSerializer(AccessSerializer):
    refresh = serializers.CharField()
    user = MeSerializer()


def redirect_uri_for(provider: str) -> str:
    """The configured Next callback of ``provider`` (admin contract §2.1), ``""`` when not configured.

    ``GOOGLE_OAUTH_REDIRECT_URI`` / ``FACEBOOK_OAUTH_REDIRECT_URI``; the settings module already falls back to
    the deprecated single ``OAUTH_REDIRECT_URI`` (one release), and so does an override of only that value.
    """
    specific = {
        "google": getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", ""),
        "facebook": getattr(settings, "FACEBOOK_OAUTH_REDIRECT_URI", ""),
    }.get(provider, "")
    return str(specific or getattr(settings, "OAUTH_REDIRECT_URI", ""))


class OAuthCallbackSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST auth/oauth/{provider}/`` and ``.../link/`` (contract 2.1).

    ``context["provider"]`` decides whether ``nonce`` is required (Google only). ``redirect_uri`` must equal
    the provider's configured callback (``GOOGLE_OAUTH_REDIRECT_URI`` / ``FACEBOOK_OAUTH_REDIRECT_URI``, which
    fall back to the deprecated ``OAUTH_REDIRECT_URI``) when that is set.
    """

    code = serializers.CharField(min_length=1, max_length=2048, trim_whitespace=False)
    code_verifier = serializers.CharField(min_length=43, max_length=128, trim_whitespace=False)
    redirect_uri = serializers.URLField(max_length=2048)
    nonce = serializers.CharField(max_length=512, required=False, trim_whitespace=False)

    def validate_redirect_uri(self, value: str) -> str:
        configured = redirect_uri_for(str(self.context.get("provider", "")))
        if configured and value != configured:
            raise serializers.ValidationError([field_error("invalid")])
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.context.get("provider") == "google" and "nonce" not in attrs:
            raise serializers.ValidationError({"nonce": [field_error("required")]})
        return attrs


# --- staff directory (contract §2.16) ----------------------------------------------------------------------

STAFF_SEARCH_MAX_LENGTH = 64


class StaffListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``GET staff/``: ``search`` (name or email, trimmed, ≤ 64), ``is_active`` (default ``true``)."""

    search = serializers.CharField(required=False, max_length=STAFF_SEARCH_MAX_LENGTH, allow_blank=True)
    is_active = QueryBooleanField(required=False, default=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("search"):
            attrs.pop("search", None)
        return attrs


class StaffListItemSerializer(ActorSerializer):
    """One ``GET staff/`` row: the ``Actor`` keys plus ``email`` and ``is_active``."""

    email = serializers.EmailField()
    is_active = serializers.BooleanField()
