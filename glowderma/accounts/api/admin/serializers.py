from dataclasses import asdict
from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.accounts import selectors
from glowderma.accounts.models import AuthSession
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import QueryBooleanField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.uzsource import N_


class LoginSerializer(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class RefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh = serializers.CharField(trim_whitespace=False)


class LogoutSerializer(RefreshSerializer):
    scope = serializers.ChoiceField(choices=["current", "others", "all"], default="current")


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
MANAGER_ONLY_KEYS = (
    "full_name",
    "is_manager",
    "last_login_at",
    "linked_providers",
    "created_at",
    "must_change_password",
    "mfa_enabled",
    "invite",
)


def linked_providers(user: Any) -> list[str]:
    """The providers of the user's prefetched social identities (``selectors.with_member_columns``)."""
    return [identity.provider for identity in user.social_identities.all()]


class StaffInviteSerializer(serializers.Serializer[dict[str, Any]]):
    """``StaffMember.invite``: the newest password link (invite or reset) of the account."""

    state = serializers.ChoiceField(
        choices=enums.StaffInviteState.values,
        help_text="`none` (never e-mailed a link), `pending` (open: queued or sent), `expired`, `used` (the "
        "person set a password with it), `revoked` (replaced by a newer link, a password change, a "
        "temporary-password reset or a deactivation).",
    )
    sent_at = serializers.DateTimeField(
        allow_null=True, help_text="When the e-mail went to the relay; `null` while queued or when not sent."
    )
    expires_at = serializers.DateTimeField(allow_null=True)


def invite_data(user: Any) -> dict[str, Any]:
    return dict(StaffInviteSerializer(asdict(selectors.invite_of(user))).data)


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
    must_change_password = serializers.BooleanField(allow_null=True, help_text="Manager only, else `null`.")
    mfa_enabled = serializers.SerializerMethodField(
        help_text="A confirmed admin panel TOTP device (the staff screen's badge and «MFA ni o‘chirish»). "
        "Manager only, else `null`."
    )
    invite = serializers.SerializerMethodField(help_text="Manager only, else `null`.")

    @extend_schema_field(
        serializers.ListField(child=serializers.ChoiceField(choices=LINKED_PROVIDERS), allow_null=True)
    )
    def get_linked_providers(self, obj: Any) -> list[str] | None:
        return linked_providers(obj)

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_mfa_enabled(self, obj: Any) -> bool | None:
        return bool(getattr(obj, "mfa_enabled", False))

    @extend_schema_field(StaffInviteSerializer(allow_null=True))
    def get_invite(self, obj: Any) -> dict[str, Any] | None:
        return invite_data(obj)

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
    must_change_password = serializers.BooleanField(
        help_text="The person must set their own password before using the admin panel."
    )
    password_changed_at = serializers.DateTimeField(
        allow_null=True,
        help_text="When the person last set their own password (a link or `auth/change-password/`); `null`: "
        "never since this was recorded.",
    )
    mfa_enabled = serializers.SerializerMethodField(help_text="A confirmed admin panel TOTP device.")
    invite = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(choices=LINKED_PROVIDERS)))
    def get_linked_providers(self, obj: Any) -> list[str]:
        return linked_providers(obj)

    @extend_schema_field(serializers.BooleanField())
    def get_mfa_enabled(self, obj: Any) -> bool:
        return bool(getattr(obj, "mfa_enabled", False))

    @extend_schema_field(StaffInviteSerializer())
    def get_invite(self, obj: Any) -> dict[str, Any]:
        return invite_data(obj)


class ActivationSerializer(serializers.Serializer[dict[str, Any]]):
    """An e-mailed password link: queued for the person's address (``sent``), valid until ``expires_at``."""

    sent = serializers.BooleanField(help_text="The e-mail was queued (delivered by the outbox).")
    expires_at = serializers.DateTimeField()


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


def _full_name_field(*, required: bool) -> serializers.CharField:
    return serializers.CharField(
        required=required,
        min_length=STAFF_FULL_NAME_MIN_LENGTH,
        max_length=STAFF_FULL_NAME_MAX_LENGTH,
    )


def _delivery_field() -> serializers.ChoiceField:
    return serializers.ChoiceField(
        choices=enums.StaffPasswordDelivery.values,
        required=False,
        default=enums.StaffPasswordDelivery.TEMPORARY_PASSWORD,
        help_text="`temporary_password` (shown once in the answer), `email_link` (a single-use link to the "
        "account's e-mail; no usable password until it is used) or `both`. The e-mail options need "
        "`Me.capabilities.staff_email_invite` (else `invalid_choice`).",
    )


class StaffCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/`` body: ``email`` (trimmed, lower-cased, unique), ``full_name`` (trimmed 2..150),
    ``is_manager`` (default ``false``), ``delivery`` (default ``temporary_password``), ``language`` (the
    person's panel and e-mail language, default ``uz``)."""

    email = serializers.EmailField(max_length=254)
    full_name = _full_name_field(required=True)
    is_manager = serializers.BooleanField(required=False, default=False)
    delivery = _delivery_field()
    language = serializers.ChoiceField(
        choices=enums.ContentLanguage.values, required=False, default=enums.ContentLanguage.UZ
    )

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class StaffResetPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/{id}/reset-password/`` body (optional): ``delivery`` (default ``temporary_password``)."""

    delivery = _delivery_field()


class StaffResetMfaSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST staff/{id}/reset-mfa/`` body (optional): ``reason`` (trimmed, up to 200 characters), kept on
    the ``auth.mfa_reset`` audit entry."""

    reason = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")


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


# --- admin panel MFA (feat/admin-ux, contract §2.1 "MFA") ---------------------------------------------------

MFA_CODE_FIELD_LENGTH = 16
RECOVERY_CODE_FIELD_LENGTH = 32


def _password_field() -> serializers.CharField:
    return serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


def _code_field(help_text: str, *, required: bool = True) -> serializers.CharField:
    return serializers.CharField(
        max_length=MFA_CODE_FIELD_LENGTH, required=required, write_only=True, help_text=help_text
    )


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

    current_password = _password_field()


class MfaSetupSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/setup/`` (200): the pending device to scan (QR of ``otpauth_uri``) or type."""

    device_id = serializers.IntegerField()
    otpauth_uri = serializers.CharField(help_text='`otpauth://totp/…`, issuer "Glow Derma Admin".')
    manual_key = serializers.CharField(help_text="The secret in base32, in groups of four.")


class MfaConfirmSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/confirm/`` body."""

    device_id = serializers.IntegerField(min_value=1)
    code = _code_field("The 6-digit code the authenticator shows now.")


class MfaRecoveryCodesSerializer(serializers.Serializer[dict[str, Any]]):
    """``auth/mfa/confirm/`` and ``auth/mfa/recovery-codes/`` (200): shown once, never again."""

    recovery_codes = serializers.ListField(
        child=serializers.CharField(), help_text="Ten `xxxxx-xxxxx` codes."
    )


class MfaDisableSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/disable/`` body."""

    current_password = _password_field()
    code_or_recovery = serializers.CharField(
        max_length=RECOVERY_CODE_FIELD_LENGTH,
        write_only=True,
        help_text="The authenticator's 6-digit code or one unused recovery code.",
    )


class MfaRegenerateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/recovery-codes/`` body."""

    current_password = _password_field()
    code = _code_field("The 6-digit code the authenticator shows now.")


class MfaVerifySerializer(serializers.Serializer[dict[str, Any]]):
    """``POST auth/mfa/verify/`` body: the challenge and exactly one of ``code`` / ``recovery_code``."""

    challenge = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    code = _code_field("The authenticator's 6-digit code.", required=False)
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
