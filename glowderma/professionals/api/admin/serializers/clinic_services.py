"""A specialist's own services (``ClinicService``): the row and its write."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import (
    i18n_dict,
    i18n_text,
)
from glowderma.professionals.api.admin.serializers.fields import MAX_POSITION, price_field, validate_duration
from glowderma.professionals.api.admin.serializers.texts import (
    ServiceDescriptionSerializer,
    ServiceNameSerializer,
    ServiceShortNameSerializer,
)
from glowderma.professionals.models import (
    Service,
)


class ClinicServiceSerializer(serializers.Serializer[Service]):
    """The admin ``ClinicService``: one service of one specialist, with the price and duration a booking
    snapshots (``is_active: false`` = not offered to customers or in the booking dialogs)."""

    id = serializers.IntegerField()
    specialist_id = serializers.IntegerField(source="professional_id")
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    category = serializers.ChoiceField(choices=enums.ServiceCategory.values)
    price = serializers.IntegerField()
    duration_min = serializers.IntegerField()
    position = serializers.IntegerField()
    is_active = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(120))
    def get_name(self, obj: Service) -> dict[str, str]:
        return i18n_dict(obj, "name")

    @extend_schema_field(i18n_text(40))
    def get_short_name(self, obj: Service) -> dict[str, str]:
        return i18n_dict(obj, "short_name")

    @extend_schema_field(i18n_text(2000))
    def get_description(self, obj: Service) -> dict[str, str]:
        return i18n_dict(obj, "description")


class ClinicServiceWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST …/services/`` and (``partial``) ``PATCH …/services/{id}/`` of one specialist (texts
    merge leaf by leaf). ``category`` defaults from the specialist's role on create."""

    name = ServiceNameSerializer()
    short_name = ServiceShortNameSerializer(required=False)
    description = ServiceDescriptionSerializer(required=False)
    category = serializers.ChoiceField(
        choices=enums.ServiceCategory.values,
        required=False,
        help_text="Defaults from the specialist's role on create.",
    )
    price = price_field(help_text="So'm, 0 … 100 000 000; snapshotted by every booking.")
    duration_min = serializers.IntegerField(
        min_value=5, max_value=480, help_text="Minutes, a multiple of 5 (5 … 480)."
    )
    position = serializers.IntegerField(
        min_value=0,
        max_value=MAX_POSITION,
        required=False,
        help_text="Order within the specialist's services; appended last on create.",
    )
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_duration_min(self, value: int) -> int:
        return cast(int, validate_duration(value))

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.partial:
            return attrs
        short = dict(attrs.get("short_name") or {})
        if not short.get("uz"):
            short["uz"] = attrs["name"]["uz"][:40].strip()
        attrs["short_name"] = {"ru": "", "en": "", **short}
        return attrs


__all__ = [
    "ClinicServiceSerializer",
    "ClinicServiceWriteSerializer",
]
