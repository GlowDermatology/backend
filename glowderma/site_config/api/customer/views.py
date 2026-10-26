"""``GET settings/public/`` (customer contract §2.4, **O**, cached 60 s)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.core import enums
from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, OPTIONAL_DEVICE_HEADERS, PUBLIC_CUSTOMER_ERRORS
from glowderma.site_config import selectors


class PlatformValuesSerializer(serializers.Serializer[dict[str, Any]]):
    ios = serializers.CharField()
    android = serializers.CharField()


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


class PublicSettingsView(PublicAPIView):
    public_cache_seconds = 60

    @extend_schema(
        operation_id="customer_settings_public_retrieve",
        parameters=[*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER],
        responses={200: PublicSettingsSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(selectors.public_settings(self.language))
