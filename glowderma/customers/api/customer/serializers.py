"""Request and response shapes of the customer auth, profile and catalog routes (customer contract §2.1–§2.3).

Request serializers raise contract field codes (``core.api.exceptions.field_error``; endpoint-specific codes
and the customer field keys of C§5.0 through ``message_key``); response serializers exist for the OpenAPI
schema only — the views return the selectors' dictionaries.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from rest_framework import serializers

from glowderma.accounts.api.throttles import canonical_phone
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, QueryBooleanField

CODE_PATTERN = re.compile(r"^[0-9]{6}$")
REFERENCE_CODE = re.compile(r"^[0-9]{1,16}$")
SEARCH_MAX_LENGTH = 64
CATEGORY_SLUG_MAX_LENGTH = 64


class CustomerPhoneField(serializers.CharField):
    """E.164 ``+998`` + nine digits (Unicode digits folded to ASCII); else ``invalid``
    (``field.phone_invalid``)."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", 32)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        phone = canonical_phone(value)
        if phone is None:
            raise serializers.ValidationError([field_error("invalid", message_key="field.phone_invalid")])
        return phone


def _password(**kwargs: Any) -> serializers.CharField:
    return serializers.CharField(max_length=128, trim_whitespace=False, write_only=True, **kwargs)


def _name(*, required: bool = True) -> serializers.CharField:
    # allow_blank: an empty or whitespace-only name reaches clean_name and answers the contract's
    # ``min_length`` (``field.name_min``), not DRF's ``blank``; a missing key still answers ``required``.
    return serializers.CharField(max_length=150, required=required, allow_null=not required, allow_blank=True)


def clean_name(value: str | None) -> str | None:
    """Names are trimmed and need at least 2 characters (C§2.1.3, §2.1.9, §2.2.2: ``min_length``
    ``field.name_min``, also for ``""`` and whitespace only)."""
    if value is None:
        return None
    value = value.strip()
    if len(value) < 2:
        raise serializers.ValidationError([field_error("min_length", message_key="field.name_min", limit=2)])
    return value


class SendCodeSerializer(serializers.Serializer[dict[str, Any]]):
    phone = CustomerPhoneField()
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    social_ticket = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=2048)

    def validate_purpose(self, value: str) -> str:
        if value == enums.OtpPurpose.LOGIN_NEW_DEVICE:  # issued only by auth/login/ after the password
            raise serializers.ValidationError([field_error("invalid_choice")])
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["purpose"] == enums.OtpPurpose.LINK_SOCIAL and not attrs.get("social_ticket"):
            raise serializers.ValidationError(
                {"social_ticket": [field_error("required", message_key="field.social_ticket_required")]}
            )
        return attrs


class VerifyCodeSerializer(serializers.Serializer[dict[str, Any]]):
    phone = CustomerPhoneField()
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    code = serializers.CharField(max_length=32)

    def validate_code(self, value: str) -> str:
        if not CODE_PATTERN.fullmatch(value):
            raise serializers.ValidationError([field_error("invalid", message_key="field.otp_format")])
        return value


class SetPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    verification_token = serializers.CharField(max_length=128)
    password = _password()
    name = _name()
    terms_version = serializers.CharField(max_length=32)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values, required=False, allow_null=True)

    def validate_name(self, value: str) -> str | None:
        return clean_name(value)


class CustomerLoginSerializer(serializers.Serializer[dict[str, Any]]):
    phone = CustomerPhoneField()
    password = _password()
    verification_token = serializers.CharField(
        max_length=128, required=False, allow_null=True, allow_blank=True
    )


class ResetPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    verification_token = serializers.CharField(max_length=128)
    password = _password()


class CustomerRefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh = serializers.CharField(max_length=256, trim_whitespace=False)


class CustomerLogoutSerializer(CustomerRefreshSerializer):
    """``auth/logout/`` (B-W1): ``current`` = this session, ``others`` = every other session, ``all`` =
    everywhere."""

    scope = serializers.ChoiceField(choices=enums.LogoutScope.values, default=enums.LogoutScope.CURRENT)


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
    verification_token = serializers.CharField(max_length=128)
    password = _password(required=False, allow_null=True)
    name = _name(required=False)
    terms_version = serializers.CharField(max_length=32, required=False, allow_null=True)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values, required=False, allow_null=True)

    def validate_name(self, value: str | None) -> str | None:
        return clean_name(value)


class MeSocialLinkSerializer(SocialSerializer):
    password = _password(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)


class ReauthSerializer(serializers.Serializer[dict[str, Any]]):
    """``DELETE me/`` and ``DELETE me/social/{provider}/``: the password, or an SMS verification token."""

    password = _password(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)


class MePasswordSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = _password(required=False, allow_null=True)
    verification_token = serializers.CharField(max_length=128, required=False, allow_null=True)
    password = _password()


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


def _coordinate(limit: int) -> serializers.FloatField:
    return serializers.FloatField(min_value=-limit, max_value=limit, required=False, allow_null=True)


class AddressWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST`` (``text`` required) and ``PATCH`` (``partial``) of ``me/addresses/`` (customer contract
    §2.2.7)."""

    label = serializers.CharField(max_length=60, required=False, allow_blank=True)  # type: ignore[assignment]
    text = serializers.CharField(max_length=300)
    # No max_length: ``^[0-9]{1,16}$`` (or "") decides, so a long value answers ``invalid`` (C§2.2.7).
    region_code = serializers.CharField(required=False, allow_blank=True)
    city_code = serializers.CharField(required=False, allow_blank=True)
    comment = serializers.CharField(max_length=200, required=False, allow_blank=True)
    latitude = _coordinate(90)
    longitude = _coordinate(180)
    is_default = serializers.BooleanField(required=False)

    def validate_text(self, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise serializers.ValidationError(
                [field_error("min_length", message_key="field.address_text_min", limit=5)]
            )
        return value

    def _reference(self, value: str) -> str:
        if value and not REFERENCE_CODE.fullmatch(value):
            raise serializers.ValidationError([field_error("invalid")])
        return value

    def validate_region_code(self, value: str) -> str:
        return self._reference(value)

    def validate_city_code(self, value: str) -> str:
        return self._reference(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        lat_given, lng_given = "latitude" in attrs, "longitude" in attrs
        if lat_given or lng_given:
            latitude, longitude = attrs.get("latitude"), attrs.get("longitude")
            if lat_given != lng_given or (latitude is None) != (longitude is None):
                raise serializers.ValidationError(
                    {"latitude": [field_error("latlng_pair", message_key="field.latlng_pair")]}
                )
            for name in ("latitude", "longitude"):
                value = attrs[name]
                attrs[name] = (
                    None
                    if value is None
                    else Decimal(str(value)).quantize(Decimal("0.000001"), ROUND_HALF_UP)
                )
        return attrs


class ProductListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    category = serializers.CharField(required=False)
    in_stock = QueryBooleanField(required=False)
    promo = QueryBooleanField(required=False)
    ordering = serializers.ChoiceField(
        choices=enums.CustomerProductOrdering.values,
        required=False,
        default=enums.CustomerProductOrdering.DEFAULT,
    )

    def validate_search(self, value: str) -> str:
        return value.strip()[:SEARCH_MAX_LENGTH]

    def validate_category(self, value: str) -> str:
        # An over-long slug can match no category: ``invalid_choice`` like any unknown slug (C§2.3).
        if len(value) > CATEGORY_SLUG_MAX_LENGTH:
            raise serializers.ValidationError([field_error("invalid_choice")])
        return value


# --- response shapes (OpenAPI only) ------------------------------------------------------------------------


class CustomerAddressSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    label = serializers.CharField()  # type: ignore[assignment]
    text = serializers.CharField()
    region_code = serializers.CharField()
    city_code = serializers.CharField()
    comment = serializers.CharField()
    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)
    is_default = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class CustomerAddressListSerializer(serializers.Serializer[dict[str, Any]]):
    results = CustomerAddressSerializer(many=True)


class ConsentStateSerializer(serializers.Serializer[dict[str, Any]]):
    terms_version = serializers.CharField(allow_null=True)
    accepted_at = serializers.DateTimeField(allow_null=True)
    current_version = serializers.CharField(allow_null=True)
    update_required = serializers.BooleanField()


class CustomerMeSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    phone = serializers.CharField()
    name = serializers.CharField()
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values)
    image_url = serializers.URLField(allow_null=True)
    registered_at = serializers.DateTimeField()
    has_password = serializers.BooleanField()
    linked_providers = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.CustomerSocialProvider.values)
    )
    push_enabled = serializers.BooleanField()
    consent = ConsentStateSerializer()
    default_address = CustomerAddressSerializer(allow_null=True)


class AuthTokensSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh = serializers.CharField()
    refresh_expires_at = serializers.DateTimeField()
    device_key = serializers.CharField(allow_null=True)
    customer = CustomerMeSerializer()
    is_new_customer = serializers.BooleanField()
    reactivated = serializers.BooleanField()


class OtpInfoSerializer(serializers.Serializer[dict[str, Any]]):
    expires_in = serializers.IntegerField()
    resend_in = serializers.IntegerField()
    length = serializers.IntegerField()


class NextStepSerializer(serializers.Serializer[dict[str, Any]]):
    next_step = serializers.ChoiceField(choices=enums.NextStep.values)
    phone_mask = serializers.CharField(allow_null=True)
    otp = OtpInfoSerializer(allow_null=True)
    social_ticket = serializers.CharField(allow_null=True)
    provider = serializers.ChoiceField(choices=enums.CustomerSocialProvider.values, allow_null=True)
    email_mask = serializers.CharField(allow_null=True)
    expires_in = serializers.IntegerField()


class CodeIssueSerializer(serializers.Serializer[dict[str, Any]]):
    phone_mask = serializers.CharField()
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    expires_in = serializers.IntegerField()
    resend_in = serializers.IntegerField()
    attempts = serializers.IntegerField()
    length = serializers.IntegerField()


class VerificationSerializer(serializers.Serializer[dict[str, Any]]):
    verification_token = serializers.CharField()
    expires_in = serializers.IntegerField()
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    customer_exists = serializers.BooleanField()


class RefreshResponseSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh = serializers.CharField()
    refresh_expires_at = serializers.DateTimeField()


class NonceSerializer(serializers.Serializer[dict[str, Any]]):
    nonce = serializers.CharField()
    expires_in = serializers.IntegerField()


class ImageUrlSerializer(serializers.Serializer[dict[str, Any]]):
    image_url = serializers.URLField()


class DeviceStateSerializer(serializers.Serializer[dict[str, Any]]):
    fid = serializers.CharField()
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values)
    model = serializers.CharField()
    os = serializers.CharField()
    app_version = serializers.CharField()
    push_enabled = serializers.BooleanField()
    last_active_at = serializers.DateTimeField()


class ProductCategorySerializer(serializers.Serializer[dict[str, Any]]):
    slug = serializers.CharField()
    name = serializers.CharField()


class ProductCardSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    slug = serializers.CharField(help_text="The website URL segment (B-W1); `catalog/products/{slug}/`.")
    name = serializers.CharField()
    volume = serializers.CharField()
    price = serializers.IntegerField()
    promo = serializers.BooleanField()
    category = ProductCategorySerializer(allow_null=True)
    main_image_url = serializers.URLField(allow_null=True)
    available = serializers.IntegerField()
    stock_status = serializers.ChoiceField(choices=enums.StockStatus.values)
    in_cart_quantity = serializers.IntegerField()
    updated_at = AwareDateTimeField(help_text="Last change of the product page (sitemap `lastmod`).")


class CustomerProductImageSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    url = serializers.URLField()
    position = serializers.IntegerField()


class ProductDetailSerializer(ProductCardSerializer):
    description = serializers.CharField()
    ingredients = serializers.CharField()
    usage = serializers.CharField()
    images = CustomerProductImageSerializer(many=True)
    max_quantity = serializers.IntegerField()
    seo_title = serializers.CharField(help_text='Staff-written page title in this language; "" = none.')
    seo_description = serializers.CharField(help_text='Staff-written meta description; "" = none.')


class CustomerBannerLinkSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.BannerLinkKind.values)
    product_id = serializers.IntegerField(allow_null=True)
    category = serializers.CharField(allow_null=True)
    url = serializers.CharField(
        allow_null=True, help_text="Kind `url`: the https link the app opens; else null."
    )


class CustomerBannerSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    title = serializers.CharField()
    image_url = serializers.URLField(allow_null=True)
    image_width = serializers.IntegerField()
    image_height = serializers.IntegerField()
    link = CustomerBannerLinkSerializer()


class CustomerBannerListSerializer(serializers.Serializer[dict[str, Any]]):
    results = CustomerBannerSerializer(many=True)


class CustomerCategorySerializer(serializers.Serializer[dict[str, Any]]):
    slug = serializers.CharField()
    name = serializers.CharField()
    image_url = serializers.URLField(allow_null=True)
    product_count = serializers.IntegerField()
    updated_at = AwareDateTimeField()
    seo_title = serializers.CharField()
    seo_description = serializers.CharField()


class CustomerCategoryListSerializer(serializers.Serializer[dict[str, Any]]):
    results = CustomerCategorySerializer(many=True)
