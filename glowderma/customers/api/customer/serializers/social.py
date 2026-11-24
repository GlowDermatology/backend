"""Social sign-in and linking shapes (``auth/social/*``, ``me/social/``, customer contract §2.1.8, §2.2.8)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.customers.api.customer.serializers.fields import clean_name, name_field, password_field


class SocialSerializer(serializers.Serializer[dict[str, Any]]):
    provider = serializers.ChoiceField(choices=enums.CustomerSocialProvider.values)
    token = serializers.CharField(max_length=4096, trim_whitespace=False)
    token_type = serializers.ChoiceField(
        choices=enums.SocialTokenType.values, required=False, allow_null=True
    )
    nonce = serializers.CharField(max_length=512, required=False, allow_null=True, trim_whitespace=False)
    authorization_code = serializers.CharField(
        max_length=1024, required=False, allow_null=True, trim_whitespace=False
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["provider"] == enums.CustomerSocialProvider.APPLE and not attrs.get("authorization_code"):
            raise serializers.ValidationError({"authorization_code": [field_error("required")]})
        return attrs


class SocialCompleteSerializer(serializers.Serializer[dict[str, Any]]):
    social_ticket = serializers.CharField(max_length=2048)
    # null = sign up a new account without a phone (hybrid identity, C§2.1.9)
    verification_token = serializers.CharField(
        max_length=128, required=False, allow_null=True, allow_blank=True
    )
    password = password_field(required=False, allow_null=True)
    name = name_field(required=False)
    terms_version = serializers.CharField(max_length=32, required=False, allow_null=True)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values, required=False, allow_null=True)

    def validate_name(self, value: str | None) -> str | None:
        return clean_name(value)


class MeSocialLinkSerializer(SocialSerializer):
    password = password_field(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)


class NonceSerializer(serializers.Serializer[dict[str, Any]]):
    nonce = serializers.CharField()
    expires_in = serializers.IntegerField()


__all__ = [
    "MeSocialLinkSerializer",
    "NonceSerializer",
    "SocialCompleteSerializer",
    "SocialSerializer",
]
