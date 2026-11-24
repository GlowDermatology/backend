"""A specialist's blocked periods."""

from __future__ import annotations

import datetime as dt
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums, timeutils
from glowderma.core.api.fields import (
    AwareDateTimeField,
)
from glowderma.core.api.serializers import ActorSerializer
from glowderma.professionals.api.admin.serializers.fields import field_refusal
from glowderma.professionals.api.admin.serializers.messages import MESSAGES
from glowderma.professionals.models import (
    BlockedPeriod,
)
from glowderma.professionals.services import (
    BLOCK_MAX_DAYS,
    DURATION_STEP_MIN,
)


class BlockedPeriodSerializer(serializers.Serializer[BlockedPeriod]):
    """An absolute busy / off range of the specialist (``label`` null = none)."""

    id = serializers.IntegerField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    kind = serializers.ChoiceField(choices=enums.BlockKind.values)
    label = serializers.SerializerMethodField()  # type: ignore[assignment]  # a field, not Field.label
    created_by = ActorSerializer(allow_null=True)
    created_at = serializers.DateTimeField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_label(self, obj: BlockedPeriod) -> str | None:
        return obj.label or None


class BlockedPeriodCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST specialists/{id}/blocks/``."""

    start = AwareDateTimeField()
    end = AwareDateTimeField()
    kind = serializers.ChoiceField(choices=enums.BlockKind.values)
    label = serializers.CharField(  # type: ignore[assignment]  # a field, not Field.label
        max_length=80, allow_blank=True, allow_null=True, required=False
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start: dt.datetime = attrs["start"]
        end: dt.datetime = attrs["end"]
        for name, value in (("start", start), ("end", end)):
            # The slot engine reads blocks as Tashkent "HH:MM"; an off-grid edge would leave a bookable
            # slot overlapping the block, so both edges sit on the 5-minute grid like the weekly hours.
            local = value.astimezone(timeutils.TASHKENT)
            if local.second or local.microsecond or local.minute % DURATION_STEP_MIN:
                raise field_refusal(name, "invalid", MESSAGES["grid"])
        if end <= start:
            raise field_refusal("end", "invalid", MESSAGES["end_before_start"])
        if end <= timeutils.now():
            raise field_refusal("end", "in_past", MESSAGES["block_past"])
        if end - start > dt.timedelta(days=BLOCK_MAX_DAYS):
            raise field_refusal("end", "out_of_range", MESSAGES["block_long"])
        return {**attrs, "label": (attrs.get("label") or "").strip()}


__all__ = [
    "BlockedPeriodCreateSerializer",
    "BlockedPeriodSerializer",
]
