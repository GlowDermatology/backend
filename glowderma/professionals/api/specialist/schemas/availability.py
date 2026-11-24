"""OpenAPI declarations for the specialist's availability: the weekly hours, the date overrides and the
blocked periods."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.api.specialist.serializers import (
    SpecialistAvailabilitySerializer,
    SpecialistOwnProfileSerializer,
)

AVAILABILITY_GET = extend_schema(
    operation_id="specialist_availability",
    parameters=[ScheduleRangeQuerySerializer],
    responses={200: SpecialistAvailabilitySerializer, **ADMIN_ERRORS},
)

WEEKLY_HOURS_PUT = extend_schema(
    operation_id="specialist_weekly_hours_update",
    description=(
        "The whole week at the assigned clinics: a weekday left out is not a working day. Appointments "
        "already booked outside the new hours are kept (see `specialist/conflicts/`)."
    ),
    request=WorkingWeekSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
)

OVERRIDES_POST = extend_schema(
    operation_id="specialist_override_create",
    request=HoursOverrideCreateSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={201: HoursOverrideSerializer, **ADMIN_ERRORS},
)

OVERRIDES_PUT = extend_schema(
    operation_id="specialist_override_update",
    request=HoursOverrideCreateSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: HoursOverrideSerializer, **ADMIN_ERRORS},
)

OVERRIDES_DELETE = extend_schema(
    operation_id="specialist_override_delete",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={204: None, **ADMIN_ERRORS},
)

BLOCKS_POST = extend_schema(
    operation_id="specialist_block_create",
    request=BlockedPeriodCreateSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={201: BlockedPeriodSerializer, **ADMIN_ERRORS},
)

BLOCKS_PUT = extend_schema(
    operation_id="specialist_block_update",
    request=BlockedPeriodCreateSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: BlockedPeriodSerializer, **ADMIN_ERRORS},
)

BLOCKS_DELETE = extend_schema(
    operation_id="specialist_block_delete",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "AVAILABILITY_GET",
    "BLOCKS_DELETE",
    "BLOCKS_POST",
    "BLOCKS_PUT",
    "OVERRIDES_DELETE",
    "OVERRIDES_POST",
    "OVERRIDES_PUT",
    "WEEKLY_HOURS_PUT",
]
