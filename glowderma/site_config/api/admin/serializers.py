"""Admin serializers of `site_config` (contract §2.11)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums, integration_modes
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import PhoneField, i18n_dict, i18n_text
from glowderma.core.api.serializers import ActorSerializer, ProviderModesSerializer
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.services import CONTACT_FIELDS, TEXT_DOCUMENTS

URL_MAX_LENGTH = 200  # SiteSettings.*_url
LEGAL_TEXT_MAX_LENGTH = 50_000  # terms and privacy policy, per language
BOOKING_POLICY_MAX_LENGTH = 5_000
TEXT_MAX_LENGTHS: dict[str, int] = {
    "terms": LEGAL_TEXT_MAX_LENGTH,
    "privacy_policy": LEGAL_TEXT_MAX_LENGTH,
    "booking_policy": BOOKING_POLICY_MAX_LENGTH,
}
# Read-only keys a PATCH must not send (``read_only`` field error).
READ_ONLY_KEYS: tuple[str, ...] = ("integrations", "terms_version", "terms_updated_at")

URL_INVALID = "Havolani https:// bilan to‘liq kiriting."
TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "www.telegram.me"})
TELEGRAM_INVALID = "Telegram havolasi https://t.me/… ko‘rinishida bo‘lsin."
INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
INSTAGRAM_INVALID = "Instagram havolasi https://instagram.com/… ko‘rinishida bo‘lsin."


class HttpsUrlField(serializers.CharField):
    """An absolute ``https`` URL (≤ 200, trimmed) without credentials or a non-default port; ``hosts``
    (optional) limits the host and then requires an account path (``https://t.me/glowderma``). ``null`` and
    ``""`` clear the value (stored ``""``, served ``null``)."""

    def __init__(
        self, *, hosts: frozenset[str] | None = None, host_message: str = URL_INVALID, **kwargs: Any
    ) -> None:
        kwargs.setdefault("max_length", URL_MAX_LENGTH)
        kwargs.setdefault("allow_blank", True)
        kwargs.setdefault("allow_null", True)
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)
        self.hosts = hosts
        self.host_message = host_message

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        try:
            URLValidator(schemes=["https"])(value)
            parts = urlsplit(value)
            port = parts.port
        except DjangoValidationError, ValueError:
            raise serializers.ValidationError(field_error("invalid", URL_INVALID)) from None
        if parts.username is not None or parts.password is not None or port not in (None, 443):
            raise serializers.ValidationError(field_error("invalid", URL_INVALID))
        if self.hosts is not None and ((parts.hostname or "") not in self.hosts or not parts.path.strip("/")):
            raise serializers.ValidationError(field_error("invalid", self.host_message))
        return value


class LongTextField(serializers.CharField):
    """A trimmed text whose line breaks are stored as ``\\n`` (so its audited digest does not depend on the
    browser's ``\\r\\n``)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("allow_blank", True)
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()


class PickupAddressSerializer(serializers.Serializer[dict[str, str]]):
    uz = serializers.CharField(max_length=200, allow_blank=True, required=False)
    ru = serializers.CharField(max_length=200, allow_blank=True, required=False)
    en = serializers.CharField(max_length=200, allow_blank=True, required=False)


class LegalTextPatchSerializer(serializers.Serializer[dict[str, str]]):
    """``terms`` / ``privacy_policy`` of a PATCH: each language optional, ≤ 50 000 characters, trimmed."""

    uz = LongTextField(max_length=LEGAL_TEXT_MAX_LENGTH)
    ru = LongTextField(max_length=LEGAL_TEXT_MAX_LENGTH)
    en = LongTextField(max_length=LEGAL_TEXT_MAX_LENGTH)


class BookingPolicyPatchSerializer(serializers.Serializer[dict[str, str]]):
    """``booking_policy`` of a PATCH: each language optional, ≤ 5 000 characters, trimmed."""

    uz = LongTextField(max_length=BOOKING_POLICY_MAX_LENGTH)
    ru = LongTextField(max_length=BOOKING_POLICY_MAX_LENGTH)
    en = LongTextField(max_length=BOOKING_POLICY_MAX_LENGTH)


class IntegrationsSerializer(serializers.Serializer[dict[str, Any]]):
    """Read-only: ``mode`` = ``INTEGRATIONS_MODE`` (the default of every provider), ``providers`` = the
    effective mode of each provider."""

    mode = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    providers = ProviderModesSerializer()


class SettingsSerializer(serializers.Serializer[SiteSettings]):
    """``GET settings/`` (contract §2.11)."""

    reservation_minutes = serializers.IntegerField()
    support_phone = serializers.SerializerMethodField()
    store_phone = serializers.SerializerMethodField()
    cosmetologist_phone = serializers.SerializerMethodField()
    telegram_url = serializers.SerializerMethodField()
    instagram_url = serializers.SerializerMethodField()
    website_url = serializers.SerializerMethodField()
    pickup_address = serializers.SerializerMethodField()
    terms = serializers.SerializerMethodField()
    privacy_policy = serializers.SerializerMethodField()
    booking_policy = serializers.SerializerMethodField()
    terms_version = serializers.SerializerMethodField()
    terms_updated_at = serializers.DateTimeField(allow_null=True)
    integrations = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()
    updated_by = ActorSerializer(allow_null=True)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_support_phone(self, obj: SiteSettings) -> str | None:
        return obj.support_phone or None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_store_phone(self, obj: SiteSettings) -> str | None:
        return obj.store_phone or None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_cosmetologist_phone(self, obj: SiteSettings) -> str | None:
        return obj.cosmetologist_phone or None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_telegram_url(self, obj: SiteSettings) -> str | None:
        return obj.telegram_url or None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_instagram_url(self, obj: SiteSettings) -> str | None:
        return obj.instagram_url or None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_website_url(self, obj: SiteSettings) -> str | None:
        return obj.website_url or None

    @extend_schema_field(PickupAddressSerializer())
    def get_pickup_address(self, obj: SiteSettings) -> dict[str, str]:
        return i18n_dict(obj, "pickup_address")

    @extend_schema_field(i18n_text(LEGAL_TEXT_MAX_LENGTH))
    def get_terms(self, obj: SiteSettings) -> dict[str, str]:
        return i18n_dict(obj, "terms")

    @extend_schema_field(i18n_text(LEGAL_TEXT_MAX_LENGTH))
    def get_privacy_policy(self, obj: SiteSettings) -> dict[str, str]:
        return i18n_dict(obj, "privacy_policy")

    @extend_schema_field(i18n_text(BOOKING_POLICY_MAX_LENGTH))
    def get_booking_policy(self, obj: SiteSettings) -> dict[str, str]:
        return i18n_dict(obj, "booking_policy")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_terms_version(self, obj: SiteSettings) -> str | None:
        return obj.terms_version or None

    @extend_schema_field(IntegrationsSerializer())
    def get_integrations(self, obj: SiteSettings) -> dict[str, Any]:
        return {"mode": str(settings.INTEGRATIONS_MODE), "providers": integration_modes.provider_modes()}


class SettingsPatchSerializer(serializers.Serializer[dict[str, Any]]):
    """``PATCH settings/`` (contract §2.11): ``reservation_minutes`` 5..60; the phones E.164 | null; the links
    https | null (Telegram ``t.me`` / ``telegram.me``, Instagram ``instagram.com``); ``pickup_address.*``
    ≤ 200; ``terms.*`` / ``privacy_policy.*`` ≤ 50 000 and ``booking_policy.*`` ≤ 5 000, trimmed (each
    language optional); ``integrations``, ``terms_version`` and ``terms_updated_at`` are read-only
    (``read_only`` field error)."""

    reservation_minutes = serializers.IntegerField(min_value=5, max_value=60, required=False)
    support_phone = PhoneField(required=False, allow_null=True)
    store_phone = PhoneField(required=False, allow_null=True)
    cosmetologist_phone = PhoneField(required=False, allow_null=True)
    telegram_url = HttpsUrlField(hosts=TELEGRAM_HOSTS, host_message=TELEGRAM_INVALID)
    instagram_url = HttpsUrlField(hosts=INSTAGRAM_HOSTS, host_message=INSTAGRAM_INVALID)
    website_url = HttpsUrlField()
    pickup_address = PickupAddressSerializer(required=False)
    terms = LegalTextPatchSerializer(required=False)
    privacy_policy = LegalTextPatchSerializer(required=False)
    booking_policy = BookingPolicyPatchSerializer(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        data = self.initial_data
        if isinstance(data, dict):
            errors = {key: [field_error("read_only")] for key in READ_ONLY_KEYS if key in data}
            if errors:
                raise serializers.ValidationError(errors)
        return attrs

    def to_changes(self) -> dict[str, Any]:
        """Column name → value for ``services.update_settings`` (a contact may be ``None``)."""
        data = self.validated_data
        changes: dict[str, Any] = {}
        if "reservation_minutes" in data:
            changes["reservation_minutes"] = data["reservation_minutes"]
        for name in CONTACT_FIELDS:
            if name in data:
                changes[name] = data[name] or None
        for field in ("pickup_address", *TEXT_DOCUMENTS):
            for language, value in data.get(field, {}).items():
                changes[f"{field}_{language}"] = value
        return changes
