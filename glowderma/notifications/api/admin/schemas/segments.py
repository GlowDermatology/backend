"""OpenAPI declarations for customer segments: list (with its export), create, update and delete."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
)
from glowderma.notifications.api.admin.serializers import (
    SegmentSerializer,
    SegmentWriteSerializer,
)

SEGMENT_LIST = extend_schema(
    operation_id="admin_segments_list",
    parameters=[EXPORT_PARAM],
    responses={200: SegmentSerializer(many=True), **ADMIN_ERRORS},
)

SEGMENT_CREATE = extend_schema(
    operation_id="admin_segments_create",
    request=SegmentWriteSerializer,
    responses={201: SegmentSerializer, **ADMIN_ERRORS},
)

SEGMENT_RETRIEVE = extend_schema(
    operation_id="admin_segments_retrieve", responses={200: SegmentSerializer, **ADMIN_ERRORS}
)

SEGMENT_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_segments_partial_update",
    description=(
        "A rule change answers 409 `conflict` while an approved `scheduled` notification targets the "
        "segment (its audience must not grow before the dispatch); renaming is always allowed."
    ),
    request=SegmentWriteSerializer,
    responses={200: SegmentSerializer, **ADMIN_ERRORS},
)

SEGMENT_DESTROY = extend_schema(operation_id="admin_segments_destroy", responses={204: None, **ADMIN_ERRORS})


__all__ = [
    "SEGMENT_CREATE",
    "SEGMENT_DESTROY",
    "SEGMENT_LIST",
    "SEGMENT_PARTIAL_UPDATE",
    "SEGMENT_RETRIEVE",
]
