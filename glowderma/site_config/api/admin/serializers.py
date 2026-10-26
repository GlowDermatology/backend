"""Admin serializers of `site_config` (contract §2.11) and the shared ``ActorRef`` shape."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums, integration_modes
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import PhoneField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.site_config.models import SiteSettings


class PickupAddressSerializer(serializers.Serializer[dict[str, str]]):
    uz = serializers.CharField(max_length=200, allow_blank=True, required=False)
    ru = serializers.CharField(max_length=200, allow_blank=True, required=False)
    en = serializers.CharField(max_length=200, allow_blank=True, required=False)


class ProviderModesSerializer(serializers.Serializer[dict[str, str]]):
    """Per-provider adapter modes (``*_MODE`` settings, each defaulting to ``INTEGRATIONS_MODE``)."""

    oauth = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    billz = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    fcm = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    click = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    payme = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    bts = serializers.ChoiceField(choices=enums.IntegrationMode.values)


class IntegrationsSerializer(serializers.Serializer[dict[str, Any]]):
    """Read-only: ``mode`` = ``INTEGRATIONS_MODE`` (the default of every provider), ``providers`` = the
    effective mode of each provider."""

    mode = serializers.ChoiceField(choices=enums.IntegrationMode.values)
    providers = ProviderModesSerializer()


class SettingsSerializer(serializers.Serializer[SiteSettings]):
    """``GET settings/`` (contract §2.11)."""

    reservation_minutes = serializers.IntegerField()
    support_phone = serializers.SerializerMethodField()
    pickup_address = serializers.SerializerMethodField()
    integrations = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()
    updated_by = ActorSerializer(allow_null=True)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_support_phone(self, obj: SiteSettings) -> str | None:
        return obj.support_phone or None

    @extend_schema_field(PickupAddressSerializer())
    def get_pickup_address(self, obj: SiteSettings) -> dict[str, str]:
        return {
            "uz": obj.pickup_address_uz,
            "ru": obj.pickup_address_ru,
            "en": obj.pickup_address_en,
        }

    @extend_schema_field(IntegrationsSerializer())
    def get_integrations(self, obj: SiteSettings) -> dict[str, Any]:
        return {"mode": str(settings.INTEGRATIONS_MODE), "providers": integration_modes.provider_modes()}


class SettingsPatchSerializer(serializers.Serializer[dict[str, Any]]):
    """``PATCH settings/``: ``reservation_minutes`` 5..60, ``support_phone`` E.164 | null,
    ``pickup_address.*`` ≤ 200; ``integrations`` is read-only (``read_only`` field error)."""

    reservation_minutes = serializers.IntegerField(min_value=5, max_value=60, required=False)
    support_phone = PhoneField(required=False, allow_null=True)
    pickup_address = PickupAddressSerializer(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        data = self.initial_data
        if isinstance(data, dict) and "integrations" in data:
            raise serializers.ValidationError({"integrations": [field_error("read_only")]})
        return attrs

    def to_changes(self) -> dict[str, Any]:
        """Column name → value for ``services.update_settings``."""
        data = self.validated_data
        changes: dict[str, Any] = {}
        if "reservation_minutes" in data:
            changes["reservation_minutes"] = data["reservation_minutes"]
        if "support_phone" in data:
            changes["support_phone"] = data["support_phone"]
        for language, value in data.get("pickup_address", {}).items():
            changes[f"pickup_address_{language}"] = value
        return changes
