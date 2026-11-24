"""The date range filter of the schedule lists (overrides and blocked periods)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from rest_framework import serializers

from glowderma.core import timeutils
from glowderma.professionals.api.admin.serializers.fields import field_refusal
from glowderma.professionals.api.admin.serializers.messages import MESSAGES

RANGE_LIMIT_DAYS = 3660  # schedule list filters: today ± ~10 years (keeps day_bounds away from date.max)


class ScheduleRangeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``date_from`` (default today, Tashkent) … ``date_to`` (optional, inclusive)."""

    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        today = timeutils.local_date(timeutils.now())
        limit = dt.timedelta(days=RANGE_LIMIT_DAYS)
        for name in ("date_from", "date_to"):
            value = attrs.get(name)
            if value is not None and not today - limit <= value <= today + limit:
                raise field_refusal(name, "out_of_range", MESSAGES["range_far"])
        date_from = attrs.get("date_from") or today
        date_to = attrs.get("date_to")
        if date_to is not None and date_to < date_from:
            raise field_refusal("date_to", "invalid", MESSAGES["range"])
        return {"date_from": date_from, "date_to": date_to}


__all__ = [
    "RANGE_LIMIT_DAYS",
    "ScheduleRangeQuerySerializer",
]
