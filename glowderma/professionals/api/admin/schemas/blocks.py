"""OpenAPI declarations for A specialist's blocked periods."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    ScheduleRangeQuerySerializer,
)

SPECIALIST_BLOCK_LIST = extend_schema(
    operation_id="admin_specialists_blocks_list",
    description="Blocked periods touching the days `date_from` … `date_to`, by start; default today on.",
    parameters=[ScheduleRangeQuerySerializer],
    responses={200: BlockedPeriodSerializer(many=True), **ADMIN_ERRORS},
)

SPECIALIST_BLOCK_CREATE = extend_schema(
    operation_id="admin_specialists_blocks_create",
    description="An absolute busy/off range; it may not overlap another block of the specialist.",
    request=BlockedPeriodCreateSerializer,
    responses={201: BlockedPeriodSerializer, **ADMIN_ERRORS},
)

SPECIALIST_BLOCK_DESTROY = extend_schema(
    operation_id="admin_specialists_blocks_destroy", responses={204: None, **ADMIN_ERRORS}
)


__all__ = [
    "SPECIALIST_BLOCK_CREATE",
    "SPECIALIST_BLOCK_DESTROY",
    "SPECIALIST_BLOCK_LIST",
]
