"""A specialist's free slots: the query and the answer."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.appointments import selectors
from glowderma.core.api.fields import AwareDateTimeField


class SlotsQuerySerializer(serializers.Serializer[dict[str, Any]]):
    location_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    date = serializers.DateField(help_text="Tashkent calendar day, today … today + BOOKING_HORIZON_DAYS.")
    service_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)


class SlotWorkingHoursSerializer(serializers.Serializer[Any]):
    start = serializers.CharField()
    end = serializers.CharField()


class SlotSerializer(serializers.Serializer[Any]):
    start = serializers.CharField(help_text='"HH:MM" Tashkent.')
    end = serializers.CharField(help_text='"HH:MM" Tashkent.')
    start_at = serializers.DateTimeField()


class FreeSlotsSerializer(serializers.Serializer[Any]):
    date = serializers.DateField()
    specialist_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    location_id = serializers.IntegerField()
    duration_min = serializers.IntegerField()
    price = serializers.IntegerField()
    slot_step_min = serializers.IntegerField()
    working_hours = SlotWorkingHoursSerializer(allow_null=True)
    slots = SlotSerializer(many=True)


class AvailabilityQuerySerializer(serializers.Serializer[dict[str, Any]]):
    days = serializers.IntegerField(
        min_value=1,
        max_value=selectors.AVAILABILITY_MAX_DAYS,
        required=False,
        default=selectors.AVAILABILITY_DEFAULT_DAYS,
        help_text="Tashkent days from today to search for the next slot (never past the booking horizon).",
    )


class NextSlotSerializer(serializers.Serializer[Any]):
    start = AwareDateTimeField()
    location_id = serializers.IntegerField()
    service_id = serializers.IntegerField(help_text="The specialist's shortest active service.")


class SpecialistNextSlotSerializer(serializers.Serializer[Any]):
    specialist_id = serializers.IntegerField()
    next_slot = NextSlotSerializer(allow_null=True)
    slots_today = serializers.IntegerField(help_text="Free slots today for the shortest active service.")


class SpecialistNextSlotListSerializer(serializers.Serializer[Any]):
    generated_at = AwareDateTimeField()
    results = SpecialistNextSlotSerializer(many=True)


__all__ = [
    "AvailabilityQuerySerializer",
    "FreeSlotsSerializer",
    "NextSlotSerializer",
    "SpecialistNextSlotListSerializer",
    "SpecialistNextSlotSerializer",
    "SlotSerializer",
    "SlotWorkingHoursSerializer",
    "SlotsQuerySerializer",
]
