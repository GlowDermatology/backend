"""A specialist's date overrides of the weekly hours."""

from __future__ import annotations

import datetime as dt
from typing import Any

from rest_framework import serializers

from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.serializers import ActorSerializer
from glowderma.professionals.api.admin.serializers.fields import ClockField, field_refusal, id_field
from glowderma.professionals.api.admin.serializers.messages import MESSAGES
from glowderma.professionals.models import (
    WorkingHoursOverride,
)
from glowderma.professionals.services import (
    OVERRIDE_HORIZON_DAYS,
)


class HoursOverrideSerializer(serializers.Serializer[WorkingHoursOverride]):
    """One date's own hours; ``day_off`` = no hours that day (``start`` / ``end`` null)."""

    id = serializers.IntegerField()
    date = serializers.DateField()
    location_id = serializers.IntegerField(allow_null=True)
    start = serializers.TimeField(format="%H:%M", allow_null=True)
    end = serializers.TimeField(format="%H:%M", allow_null=True)
    day_off = serializers.SerializerMethodField()
    note = serializers.CharField(allow_blank=True)
    created_by = ActorSerializer(allow_null=True)
    created_at = serializers.DateTimeField()

    def get_day_off(self, obj: WorkingHoursOverride) -> bool:
        return obj.start is None or obj.end is None


class HoursOverrideCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST specialists/{id}/overrides/``: ``start`` and ``end`` both set (that date's hours) or
    both null / omitted (a day off)."""

    date = serializers.DateField(help_text="Tashkent calendar day, today … today + 365.")
    location_id = id_field(
        allow_null=True, required=False, help_text="Required when working; null on a day off."
    )
    start = ClockField(allow_null=True, required=False)
    end = ClockField(allow_null=True, required=False)
    note = serializers.CharField(max_length=120, allow_blank=True, required=False)

    def validate_date(self, value: dt.date) -> dt.date:
        today = timeutils.local_date(timeutils.now())
        if value < today:
            raise serializers.ValidationError(field_error("in_past", MESSAGES["override_past"]))
        if value > today + dt.timedelta(days=OVERRIDE_HORIZON_DAYS):
            raise serializers.ValidationError(field_error("out_of_range", MESSAGES["override_far"]))
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs.get("start"), attrs.get("end")
        if (start is None) != (end is None):
            raise field_refusal("start" if start is None else "end", "required", MESSAGES["hours_pair"])
        if start is not None and end is not None and end <= start:
            raise field_refusal("end", "invalid", MESSAGES["end_before_start"])
        location_id = attrs.get("location_id")
        if start is not None and location_id is None:
            raise field_refusal("location_id", "required")
        if start is None and location_id is not None:
            raise field_refusal("location_id", "invalid")
        return {**attrs, "start": start, "end": end, "note": attrs.get("note", "")}


__all__ = [
    "HoursOverrideCreateSerializer",
    "HoursOverrideSerializer",
]
