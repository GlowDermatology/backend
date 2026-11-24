"""OpenAPI declarations for A specialist's date overrides of the weekly hours."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.professionals.api.admin.serializers import (
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
)

SPECIALIST_OVERRIDE_LIST = extend_schema(
    operation_id="admin_specialists_overrides_list",
    description="Date overrides by date; default from today (Tashkent).",
    parameters=[ScheduleRangeQuerySerializer],
    responses={200: HoursOverrideSerializer(many=True), **ADMIN_ERRORS},
)

SPECIALIST_OVERRIDE_CREATE = extend_schema(
    operation_id="admin_specialists_overrides_create",
    description="One date's own hours, or a day off (`start` and `end` null); one per date.",
    request=HoursOverrideCreateSerializer,
    responses={201: HoursOverrideSerializer, **ADMIN_ERRORS},
)

SPECIALIST_OVERRIDE_DESTROY = extend_schema(
    operation_id="admin_specialists_overrides_destroy", responses={204: None, **ADMIN_ERRORS}
)


__all__ = [
    "SPECIALIST_OVERRIDE_CREATE",
    "SPECIALIST_OVERRIDE_DESTROY",
    "SPECIALIST_OVERRIDE_LIST",
]
