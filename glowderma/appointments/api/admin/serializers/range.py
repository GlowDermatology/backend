"""``GET appointments/range/`` (contract §2.9, feat/admin-ux: week and month boards)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.appointments.api.admin.serializers.board import (
    AppointmentClinicSerializer,
    AppointmentItemSerializer,
    DayBlockSerializer,
    WorkingHoursSerializer,
)
from glowderma.appointments.api.admin.serializers.messages import MESSAGES
from glowderma.appointments.selectors import (
    RANGE_MAX_DAYS_COUNTS,
    RANGE_MAX_DAYS_FULL,
    RangeDay,
    RangeSchedule,
    RangeView,
)
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_
from glowderma.professionals.models import Professional

RANGE_MESSAGES = {
    "empty_range": N_("Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin."),
    "counts_too_long": N_("Ko‘pi bilan 42 kunlik davr."),
    "full_too_long": N_("To‘liq ko‘rinish ko‘pi bilan 7 kunlik davr uchun."),
}


class RangeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``from`` ≤ ``to`` (Tashkent days, inclusive), at most 42 days with ``detail=counts`` (default) and 7
    with ``detail=full``; ``specialist_id`` narrows to one specialist (active or not)."""

    from_ = serializers.DateField()
    to = serializers.DateField()
    specialist_id = serializers.IntegerField(required=False, min_value=1)
    detail = serializers.ChoiceField(
        choices=enums.AppointmentRangeDetail.values,
        required=False,
        default=enums.AppointmentRangeDetail.COUNTS,
    )

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        return {"from": fields.pop("from_"), **fields}  # ``from`` is a Python keyword; errors list it first

    def validate_specialist_id(self, value: int) -> int:
        if not Professional.objects.filter(pk=value).exists():
            raise serializers.ValidationError(
                [field_error("does_not_exist", MESSAGES["specialist_not_found"])]
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        first, last = attrs["from"], attrs["to"]
        if last < first:
            raise serializers.ValidationError(
                {"to": [field_error("empty_range", RANGE_MESSAGES["empty_range"])]}
            )
        span = (last - first).days + 1
        if attrs["detail"] == enums.AppointmentRangeDetail.FULL:
            if span > RANGE_MAX_DAYS_FULL:
                raise serializers.ValidationError(
                    {"to": [field_error("out_of_range", RANGE_MESSAGES["full_too_long"])]}
                )
        elif span > RANGE_MAX_DAYS_COUNTS:
            raise serializers.ValidationError(
                {"to": [field_error("out_of_range", RANGE_MESSAGES["counts_too_long"])]}
            )
        return attrs


class RangeSpecialistSerializer(serializers.Serializer[Professional]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    is_active = serializers.BooleanField()


class RangeStatusCountsSerializer(serializers.Serializer[dict[str, int]]):
    requested = serializers.IntegerField()
    confirmed = serializers.IntegerField()
    attended = serializers.IntegerField()
    rejected = serializers.IntegerField()
    cancelled = serializers.IntegerField()


class RangeDaySerializer(serializers.Serializer[RangeDay]):
    date = serializers.DateField()
    counts = RangeStatusCountsSerializer()
    total = serializers.IntegerField()
    working = serializers.BooleanField(help_text="At least one listed specialist works that day.")
    hours = serializers.SerializerMethodField()

    @extend_schema_field(
        WorkingHoursSerializer(
            allow_null=True,
            help_text="Earliest start and latest end of the listed specialists working that day; null when "
            "nobody works.",
        )
    )
    def get_hours(self, obj: RangeDay) -> dict[str, str] | None:
        return None if obj.hours is None else {"start": obj.hours[0], "end": obj.hours[1]}


class RangeScheduleSerializer(serializers.Serializer[RangeSchedule]):
    specialist_id = serializers.IntegerField()
    date = serializers.DateField()
    working_hours = serializers.SerializerMethodField()
    blocks = DayBlockSerializer(many=True, source="schedule.blocks")
    location = AppointmentClinicSerializer(source="schedule.location", allow_null=True)

    @extend_schema_field(WorkingHoursSerializer(allow_null=True))
    def get_working_hours(self, obj: RangeSchedule) -> dict[str, str] | None:
        hours = obj.schedule.working_hours
        return None if hours is None else {"start": hours[0], "end": hours[1]}


class AppointmentRangeSerializer(serializers.Serializer[RangeView]):
    """``GET appointments/range/``: ``schedules`` and ``appointments`` are present (non-null) only with
    ``detail=full``."""

    from_ = serializers.DateField(source="first")
    to = serializers.DateField(source="last")
    specialists = RangeSpecialistSerializer(many=True)
    days = RangeDaySerializer(many=True)
    schedules = RangeScheduleSerializer(many=True, allow_null=True)
    appointments = AppointmentItemSerializer(many=True, allow_null=True)

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        fields = super().get_fields()
        return {"from": fields.pop("from_"), **fields}


__all__ = [
    "RANGE_MESSAGES",
    "AppointmentRangeSerializer",
    "RangeDaySerializer",
    "RangeQuerySerializer",
    "RangeScheduleSerializer",
    "RangeSpecialistSerializer",
    "RangeStatusCountsSerializer",
]
