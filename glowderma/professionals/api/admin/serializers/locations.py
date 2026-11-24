"""The clinic locations (``ClinicLocation``): the row, the list and the writes."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core.api.fields import (
    LenientPhoneField,
    i18n_dict,
    i18n_text,
)
from glowderma.professionals.api.admin.serializers.messages import MESSAGES
from glowderma.professionals.api.admin.serializers.texts import (
    LocationAddressSerializer,
    LocationCreateAddressSerializer,
    LocationCreateNameSerializer,
    LocationDirectionsSerializer,
    LocationNameSerializer,
)
from glowderma.professionals.models import (
    Location,
)


class ClinicLocationSerializer(serializers.Serializer[Location]):
    """An admin-managed clinic branch."""

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    address = serializers.SerializerMethodField()
    directions = serializers.SerializerMethodField()
    is_active = serializers.BooleanField()
    phone = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(120))
    def get_name(self, obj: Location) -> dict[str, str]:
        return i18n_dict(obj, "name")

    @extend_schema_field(i18n_text(300))
    def get_address(self, obj: Location) -> dict[str, str]:
        return i18n_dict(obj, "address")

    @extend_schema_field(i18n_text(500))
    def get_directions(self, obj: Location) -> dict[str, str]:
        return i18n_dict(obj, "directions")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_phone(self, obj: Location) -> str | None:
        return obj.phone or None

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_latitude(self, obj: Location) -> float | None:
        return None if obj.latitude is None else round(float(obj.latitude), 6)

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_longitude(self, obj: Location) -> float | None:
        return None if obj.longitude is None else round(float(obj.longitude), 6)


class ClinicLocationsSerializer(serializers.Serializer[dict[str, Any]]):
    results = ClinicLocationSerializer(many=True)


def _coordinate(limit: int) -> serializers.DecimalField:
    return serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-limit,
        max_value=limit,
        allow_null=True,
        required=False,
        coerce_to_string=False,
        error_messages={"max_decimal_places": MESSAGES["decimals"]},
    )


class ClinicLocationPatchSerializer(serializers.Serializer[dict[str, Any]]):
    """Texts merge leaf by leaf. Coordinates must be set or cleared together."""

    name = LocationNameSerializer(required=False)
    address = LocationAddressSerializer(required=False)
    directions = LocationDirectionsSerializer(required=False)
    phone = LenientPhoneField(allow_null=True, required=False)
    latitude = _coordinate(90)
    longitude = _coordinate(180)
    is_active = serializers.BooleanField(required=False)


class ClinicLocationCreateSerializer(ClinicLocationPatchSerializer):
    name = LocationCreateNameSerializer()
    address = LocationCreateAddressSerializer()


__all__ = [
    "ClinicLocationCreateSerializer",
    "ClinicLocationPatchSerializer",
    "ClinicLocationSerializer",
    "ClinicLocationsSerializer",
]
