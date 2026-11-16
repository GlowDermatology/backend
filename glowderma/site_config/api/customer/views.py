"""``GET settings/public/`` (customer contract §2.4, **O**, cached 60 s)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.core import enums, integration_modes
from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, OPTIONAL_DEVICE_HEADERS, PUBLIC_CUSTOMER_ERRORS
from glowderma.site_config import selectors


class PlatformValuesSerializer(serializers.Serializer[dict[str, Any]]):
    ios = serializers.CharField()
    android = serializers.CharField()


class PublicDeliverySerializer(serializers.Serializer[dict[str, Any]]):
    providers = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.DeliveryProvider.values),
        help_text="Home-delivery carriers that can take an order now, in preference order.",
    )
    default_provider = serializers.ChoiceField(choices=enums.DeliveryProvider.values, allow_null=True)


def public_delivery() -> dict[str, Any]:
    """``delivery`` of ``settings/public/`` (plan §3.4): from ``core.integration_modes`` only (``site_config``
    sits below ``delivery``). Whether a Yandex method is switched on, the warehouse and the pilot list are
    decided per customer by ``delivery/calculate/``."""
    providers = integration_modes.delivery_providers()
    return {"providers": providers, "default_provider": providers[0] if providers else None}


class PublicSettingsSerializer(serializers.Serializer[dict[str, Any]]):
    support_phone = serializers.CharField(allow_null=True)
    store_phone = serializers.CharField(allow_null=True)
    cosmetologist_phone = serializers.CharField(allow_null=True)
    telegram_url = serializers.URLField(allow_null=True)
    instagram_url = serializers.URLField(allow_null=True)
    website_url = serializers.URLField(allow_null=True)
    pickup_address = serializers.CharField()
    privacy_policy = serializers.CharField()
    terms = serializers.CharField()
    booking_policy = serializers.CharField()
    terms_version = serializers.CharField(allow_null=True)
    terms_updated_at = serializers.DateTimeField(allow_null=True)
    reservation_minutes = serializers.IntegerField()
    payment_providers = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.PaymentProvider.values)
    )
    online_sales_enabled = serializers.BooleanField()
    fulfilment_methods = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    )
    min_app_version = PlatformValuesSerializer()
    store_urls = PlatformValuesSerializer()
    delivery = PublicDeliverySerializer()


class PublicSettingsView(PublicAPIView):
    public_cache_seconds = 60

    @extend_schema(
        operation_id="customer_settings_public_retrieve",
        parameters=[*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER],
        responses={200: PublicSettingsSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response({**selectors.public_settings(self.language), "delivery": public_delivery()})
