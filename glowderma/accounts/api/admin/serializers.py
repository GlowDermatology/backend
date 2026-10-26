from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

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


class AccessSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh_expires_at = serializers.DateTimeField()


class LoginResponseSerializer(AccessSerializer):
    refresh = serializers.CharField()
    user = MeSerializer()


class OAuthCallbackSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST auth/oauth/{provider}/`` and ``.../link/`` (contract 2.1).

    ``context["provider"]`` decides whether ``nonce`` is required (Google only). ``redirect_uri`` must equal
    ``settings.OAUTH_REDIRECT_URI`` when that is set.
    """

    code = serializers.CharField(min_length=1, max_length=2048, trim_whitespace=False)
    code_verifier = serializers.CharField(min_length=43, max_length=128, trim_whitespace=False)
    redirect_uri = serializers.URLField(max_length=2048)
    nonce = serializers.CharField(max_length=512, required=False, trim_whitespace=False)

    def validate_redirect_uri(self, value: str) -> str:
        configured = str(settings.OAUTH_REDIRECT_URI)
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
