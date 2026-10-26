from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_field
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


LINKED_PROVIDERS = ["google", "facebook"]
STAFF_FULL_NAME_MIN_LENGTH = 2
STAFF_FULL_NAME_MAX_LENGTH = 150
# Keys a ``StaffListItem`` fills only for a manager (``null`` for everyone else, §2.16).
MANAGER_ONLY_KEYS = ("full_name", "is_manager", "last_login_at", "linked_providers", "created_at")


def linked_providers(user: Any) -> list[str]:
    """The providers of the user's prefetched social identities (``selectors.with_member_columns``)."""
    return [identity.provider for identity in user.social_identities.all()]


class StaffListItemSerializer(ActorSerializer):
    """One ``GET staff/`` row: the ``Actor`` keys plus ``email`` and ``is_active`` for every caller (the
    audit-log actor filter), and the manager's columns, which are ``null`` unless the caller is a manager
    (``context["manager"]``)."""

    email = serializers.EmailField()
    is_active = serializers.BooleanField()
    full_name = serializers.CharField(allow_null=True, help_text="Manager only, else `null`.")
    is_manager = serializers.BooleanField(allow_null=True, help_text="Manager only, else `null`.")
    last_login_at = serializers.DateTimeField(
        allow_null=True, help_text="Manager only, else `null`; `null` for a manager too when never signed in."
    )
    linked_providers = serializers.SerializerMethodField(help_text="Manager only, else `null`.")
    created_at = serializers.DateTimeField(
        source="date_joined", allow_null=True, help_text="Manager only, else `null`."
    )

    @extend_schema_field(
        serializers.ListField(child=serializers.ChoiceField(choices=LINKED_PROVIDERS), allow_null=True)
    )
    def get_linked_providers(self, obj: Any) -> list[str] | None:
        return linked_providers(obj)

    def to_representation(self, instance: Any) -> dict[str, Any]:
        if self.context.get("manager"):
            return dict(super().to_representation(instance))
        # Built by hand so a non-manager's page never reads the manager's columns.
        return {
            "id": instance.pk,
            "display_name": self.get_display_name(instance),
            "email": instance.email,
            "is_active": instance.is_active,
            **dict.fromkeys(MANAGER_ONLY_KEYS),
        }


class StaffMemberSerializer(ActorSerializer):
    """``StaffMember`` (§2.16): a staff account as managers see it; every write of ``staff/`` returns it."""

    email = serializers.EmailField()
    full_name = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()
    is_manager = serializers.BooleanField()
    last_login_at = serializers.DateTimeField(allow_null=True, help_text="`null`: never signed in.")
    linked_providers = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source="date_joined")

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(choices=LINKED_PROVIDERS)))
    def get_linked_providers(self, obj: Any) -> list[str]:
        return linked_providers(obj)


class StaffCredentialsSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/`` (201) and ``POST staff/{id}/reset-password/`` (200): the account and its temporary
    password, shown once."""

    staff = StaffMemberSerializer()
    temporary_password = serializers.CharField(
        help_text="Shown once; the person signs in with it and changes it under «Parolni o‘zgartirish». "
        "A replay of the same `Idempotency-Key` returns it again (stored sealed, §1.7)."
    )


def _full_name_field(*, required: bool) -> serializers.CharField:
    return serializers.CharField(
        required=required,
        min_length=STAFF_FULL_NAME_MIN_LENGTH,
        max_length=STAFF_FULL_NAME_MAX_LENGTH,
    )


class StaffCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/`` body: ``email`` (trimmed, lower-cased, unique), ``full_name`` (trimmed 2..150),
    ``is_manager`` (default ``false``)."""

    email = serializers.EmailField(max_length=254)
    full_name = _full_name_field(required=True)
    is_manager = serializers.BooleanField(required=False, default=False)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


# Keys of a staff account that ``PATCH staff/{id}/`` refuses with ``read_only`` (the email is the sign-in
# identity; the state and the password have their own actions).
STAFF_READ_ONLY_KEYS = ("email", "is_active", "password")


class StaffUpdateSerializer(serializers.Serializer[dict[str, Any]]):
    """``PATCH staff/{id}/`` body: ``full_name`` and/or ``is_manager``; ``email``, ``is_active`` and
    ``password`` → ``read_only``."""

    full_name = _full_name_field(required=False)
    is_manager = serializers.BooleanField(required=False)

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
