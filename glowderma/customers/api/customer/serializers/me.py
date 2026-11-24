"""``me/`` shapes (customer contract §2.2): the profile, re-authentication, password, consent, image and
device."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.customers.api.customer.serializers.addresses import CustomerAddressSerializer
from glowderma.customers.api.customer.serializers.fields import clean_name, password_field


class ReauthSerializer(serializers.Serializer[dict[str, Any]]):
    """``DELETE me/`` and ``DELETE me/social/{provider}/``: the password, or an SMS verification token."""

    password = password_field(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)


class MePasswordSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = password_field(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)
    password = password_field()


class MePatchSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values, required=False)
    push_enabled = serializers.BooleanField(required=False)

    def validate_name(self, value: str) -> str | None:
        return clean_name(value)


class ConsentSerializer(serializers.Serializer[dict[str, Any]]):
    terms_version = serializers.CharField(max_length=32)


class DeviceSerializer(serializers.Serializer[dict[str, Any]]):
    fcm_token = serializers.CharField(max_length=512, required=False, allow_null=True, allow_blank=True)
    push_enabled = serializers.BooleanField()
    model = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    os = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    locale = serializers.CharField(max_length=8, required=False, allow_blank=True, default="")


class ConsentStateSerializer(serializers.Serializer[dict[str, Any]]):
    terms_version = serializers.CharField(allow_null=True)
    accepted_at = serializers.DateTimeField(allow_null=True)
    current_version = serializers.CharField(allow_null=True)
    update_required = serializers.BooleanField()


class CustomerMeSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    phone = serializers.CharField(
        allow_null=True, help_text="Verified phone; null until attached (checkout, pay, booking, QR need it)."
    )
    email = serializers.CharField(allow_null=True, help_text="Verified e-mail address (lower case) | null.")
    name = serializers.CharField()
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values)
    image_url = serializers.URLField(allow_null=True)
    image_thumbnail_url = serializers.URLField(allow_null=True, help_text="400 px WebP variant.")
    image_medium_url = serializers.URLField(allow_null=True, help_text="1000 px WebP variant.")
    registered_at = serializers.DateTimeField()
    has_password = serializers.BooleanField()
    linked_providers = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.CustomerSocialProvider.values)
    )
    push_enabled = serializers.BooleanField()
    consent = ConsentStateSerializer()
    default_address = CustomerAddressSerializer(allow_null=True)


class ImageUrlSerializer(serializers.Serializer[dict[str, Any]]):
    image_url = serializers.URLField()
    image_thumbnail_url = serializers.URLField(help_text="400 px WebP variant.")
    image_medium_url = serializers.URLField(help_text="1000 px WebP variant.")


class DeviceStateSerializer(serializers.Serializer[dict[str, Any]]):
    fid = serializers.CharField()
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values)
    model = serializers.CharField()
    os = serializers.CharField()
    app_version = serializers.CharField()
    push_enabled = serializers.BooleanField()
    last_active_at = serializers.DateTimeField()


__all__ = [
    "ConsentSerializer",
    "ConsentStateSerializer",
    "CustomerMeSerializer",
    "DeviceSerializer",
    "DeviceStateSerializer",
    "ImageUrlSerializer",
    "MePasswordSerializer",
    "MePatchSerializer",
    "ReauthSerializer",
]
