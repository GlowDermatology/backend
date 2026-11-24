"""Field factories and value rules shared by the admin ``professionals`` serializers: ids, the wall clock, the
duration grid and prices."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from rest_framework import serializers

from glowderma.core.api.exceptions import field_error
from glowderma.professionals.api.admin.serializers.messages import MESSAGES
from glowderma.professionals.services import (
    DURATION_STEP_MIN,
    MAX_PRICE,
)

SLOT_STEPS = (10, 15, 20, 30, 60)  # the CHECK ``professional_slot_step_allowed``


MAX_POSITION = 100_000


DB_INT_MAX = 2_147_483_647


CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def id_field(**kwargs: Any) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=1, max_value=DB_INT_MAX, **kwargs)


def field_refusal(field: str, code: str, message: str | None = None) -> serializers.ValidationError:
    return serializers.ValidationError({field: [field_error(code, message)]})


class ClockField(serializers.TimeField):
    """A Tashkent wall-clock ``"HH:MM"`` on the 5-minute grid (the admin booking grid); rendered the same."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("help_text", '"HH:MM", Tashkent, minutes a multiple of 5.')
        super().__init__(format="%H:%M", input_formats=["%H:%M"], **kwargs)

    def to_internal_value(self, value: Any) -> dt.time:
        if not isinstance(value, str) or not CLOCK.match(value):
            raise serializers.ValidationError(field_error("invalid", MESSAGES["clock"]))
        parsed = super().to_internal_value(value)
        if parsed.minute % DURATION_STEP_MIN:
            raise serializers.ValidationError(field_error("invalid", MESSAGES["grid"]))
        return parsed


def validate_duration(value: int | None) -> int | None:
    if value is not None and value % DURATION_STEP_MIN:
        raise serializers.ValidationError(field_error("invalid", MESSAGES["duration_grid"]))
    return value


def price_field(**kwargs: Any) -> serializers.IntegerField:
    return serializers.IntegerField(
        min_value=0, max_value=MAX_PRICE, error_messages={"max_value": MESSAGES["price_max"]}, **kwargs
    )


__all__ = [
    "CLOCK",
    "DB_INT_MAX",
    "MAX_POSITION",
    "SLOT_STEPS",
    "ClockField",
    "field_refusal",
    "id_field",
    "price_field",
    "validate_duration",
]
