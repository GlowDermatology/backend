"""The shapes of the public settings route (customer contract): the platform values, the delivery block and
the settings."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums


class PlatformValuesSerializer(serializers.Serializer[dict[str, Any]]):
    ios = serializers.CharField()
    android = serializers.CharField()


class PublicDeliverySerializer(serializers.Serializer[dict[str, Any]]):
    providers = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.DeliveryProvider.values),
        help_text="Home-delivery carriers that can take an order now, in preference order.",
    )
    default_provider = serializers.ChoiceField(choices=enums.DeliveryProvider.values, allow_null=True)


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
