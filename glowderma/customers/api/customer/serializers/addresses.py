"""``me/addresses/*`` shapes (customer contract §2.2.7)."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from rest_framework import serializers

from glowderma.core.api.exceptions import field_error

REFERENCE_CODE = re.compile(r"^[0-9]{1,16}$")


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


__all__ = [
    "REFERENCE_CODE",
    "AddressWriteSerializer",
    "CustomerAddressListSerializer",
    "CustomerAddressSerializer",
    "_coordinate",
]
