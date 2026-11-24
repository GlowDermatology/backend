"""The admin day board (contract §2.9): the appointment item, the specialists' days and the pending
summary."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.appointments.models import Appointment
from glowderma.appointments.selectors import (
    DayView,
)
from glowderma.core import enums
from glowderma.core.api.serializers import CustomerRefSerializer
from glowderma.professionals.models import Location
from glowderma.professionals.selectors import DaySchedule


class AppointmentClinicSerializer(serializers.Serializer[Location]):
    id = serializers.IntegerField()
    name = serializers.CharField(source="name_uz")
    address = serializers.CharField(source="address_uz", allow_blank=True)


class AppointmentItemSerializer(serializers.Serializer[Appointment]):
    """The day-item shape (contract §2.9), also returned by every action endpoint."""

    id = serializers.IntegerField()
    specialist_id = serializers.IntegerField(source="professional_id")
    customer = CustomerRefSerializer()
    service_id = serializers.IntegerField()
    service_name = serializers.CharField(source="service.name_uz")
    service_short_name = serializers.CharField(source="service.short_name_uz")
    location = AppointmentClinicSerializer()
    duration_min = serializers.IntegerField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    price = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.AppointmentStatus.values)
    requested_at = serializers.DateTimeField()
    requested_via = serializers.ChoiceField(choices=enums.RequestedVia.values)


class WorkingHoursSerializer(serializers.Serializer[tuple[str, str]]):
    start = serializers.CharField()
    end = serializers.CharField()


class DayBlockSerializer(serializers.Serializer[Any]):
    start = serializers.CharField()
    end = serializers.CharField()
    kind = serializers.ChoiceField(choices=enums.BlockKind.values)
    label = serializers.CharField(allow_null=True)  # type: ignore[assignment]  # a field, not Field.label


class SpecialistDaySerializer(serializers.Serializer[DaySchedule]):
    id = serializers.IntegerField(source="professional.pk")
    name = serializers.CharField(source="professional.name")
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values, source="professional.role")
    # false for a deactivated specialist still listed for the appointments it has that day
    is_active = serializers.BooleanField(source="professional.is_active")
    working_hours = serializers.SerializerMethodField()
    location = AppointmentClinicSerializer(allow_null=True)
    blocks = DayBlockSerializer(many=True)

    @extend_schema_field(WorkingHoursSerializer(allow_null=True))
    def get_working_hours(self, obj: DaySchedule) -> dict[str, str] | None:
        if obj.working_hours is None:
            return None
        return {"start": obj.working_hours[0], "end": obj.working_hours[1]}


class ScheduleConflictsSerializer(serializers.Serializer[dict[str, Any]]):
    """``GET specialists/{id}/conflicts/``: open future appointments the schedule no longer covers."""

    results = AppointmentItemSerializer(many=True)


class DayScheduleSerializer(serializers.Serializer[DayView]):
    """``GET appointments/?date=`` (contract §2.9)."""

    date = serializers.DateField()
    specialists = SpecialistDaySerializer(many=True)
    appointments = AppointmentItemSerializer(many=True)


class DayQuerySerializer(serializers.Serializer[dict[str, Any]]):
    date = serializers.DateField(required=False)


class PendingSummarySerializer(serializers.Serializer[dict[str, Any]]):
    count = serializers.IntegerField()
    next_at = serializers.DateTimeField(allow_null=True)


__all__ = [
    "AppointmentClinicSerializer",
    "AppointmentItemSerializer",
    "DayBlockSerializer",
    "DayQuerySerializer",
    "DayScheduleSerializer",
    "PendingSummarySerializer",
    "ScheduleConflictsSerializer",
    "SpecialistDaySerializer",
    "WorkingHoursSerializer",
]
