"""A specialist's weekly working hours: the stored day and the week a write replaces."""

from __future__ import annotations

from typing import Any, cast

from rest_framework import serializers

from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    BoundedListField,
)
from glowderma.professionals.api.admin.serializers.fields import ClockField, field_refusal, id_field
from glowderma.professionals.api.admin.serializers.messages import MESSAGES
from glowderma.professionals.models import (
    WorkingHours,
)


class WorkingDaySerializer(serializers.Serializer[WorkingHours]):
    location_id = serializers.IntegerField()
    """One working weekday (0 = Monday … 6 = Sunday) with its optional recurring break."""

    weekday = serializers.IntegerField()
    start = serializers.TimeField(format="%H:%M")
    end = serializers.TimeField(format="%H:%M")
    break_start = serializers.TimeField(format="%H:%M", allow_null=True)
    break_end = serializers.TimeField(format="%H:%M", allow_null=True)


class WorkingDayInputSerializer(serializers.Serializer[dict[str, Any]]):
    location_id = id_field()
    weekday = serializers.IntegerField(min_value=0, max_value=6, help_text="0 = Monday … 6 = Sunday.")
    start = ClockField()
    end = ClockField()
    break_start = ClockField(allow_null=True, required=False)
    break_end = ClockField(allow_null=True, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs["start"], attrs["end"]
        if end <= start:
            raise field_refusal("end", "invalid", MESSAGES["end_before_start"])
        break_start, break_end = attrs.get("break_start"), attrs.get("break_end")
        if (break_start is None) != (break_end is None):
            missing = "break_start" if break_start is None else "break_end"
            raise field_refusal(missing, "required", MESSAGES["break_pair"])
        if break_start is not None and break_end is not None:
            if break_end <= break_start:
                raise field_refusal("break_end", "invalid", MESSAGES["end_before_start"])
            if break_start < start or break_end > end:
                raise field_refusal("break_start", "invalid", MESSAGES["break_outside"])
        return {**attrs, "break_start": break_start, "break_end": break_end}


class WorkingWeekSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT specialists/{id}/working-hours/``: the working weekdays (a weekday left out is a day
    off every week)."""

    days = BoundedListField(child=WorkingDayInputSerializer(), max_items=7)

    def validate_days(self, days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        for index, day in enumerate(days):
            if day["weekday"] in seen:
                raise serializers.ValidationError(
                    cast(Any, {index: {"weekday": [field_error("unique", MESSAGES["weekday_twice"])]}})
                )
            seen.add(day["weekday"])
        return days


__all__ = [
    "WorkingDayInputSerializer",
    "WorkingDaySerializer",
    "WorkingWeekSerializer",
]
