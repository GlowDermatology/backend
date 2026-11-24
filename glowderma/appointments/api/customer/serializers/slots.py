"""A specialist's free slots: the query and the answer."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


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


__all__ = [
    "FreeSlotsSerializer",
    "SlotSerializer",
    "SlotWorkingHoursSerializer",
    "SlotsQuerySerializer",
]
