"""``appointments/meta/``: the services and specialists the booking form offers."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.appointments.api.admin.serializers.board import AppointmentClinicSerializer
from glowderma.core import enums
from glowderma.professionals.models import Professional
from glowderma.professionals.selectors import assigned_locations


class BookingServiceSerializer(serializers.Serializer[Any]):
    """One bookable service of a specialist: the snapshot an admin booking would take (contract §2.9 meta)."""

    id = serializers.IntegerField()
    name = serializers.CharField(source="name_uz")
    short_name = serializers.CharField(source="short_name_uz")
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()


class BookingSpecialistSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    services = BookingServiceSerializer(many=True, source="bookable_services")
    locations = serializers.SerializerMethodField()

    @extend_schema_field(AppointmentClinicSerializer(many=True))
    def get_locations(self, obj: Professional) -> list[dict[str, Any]]:
        items = assigned_locations(obj, active_only=True)
        serializer = AppointmentClinicSerializer(items, many=True)  # type: ignore[arg-type]
        return list(serializer.data)


class AppointmentsMetaSerializer(serializers.Serializer[dict[str, Any]]):
    specialists = BookingSpecialistSerializer(many=True)
    slot_step_min = serializers.IntegerField()


__all__ = [
    "AppointmentsMetaSerializer",
    "BookingServiceSerializer",
    "BookingSpecialistSerializer",
]
