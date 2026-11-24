"""OpenAPI declarations for A segment's members: list (with its export), add and remove."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
)
from glowderma.notifications.api.admin.serializers import (
    CustomerListItemSerializer,
    SegmentMembersInputSerializer,
    SegmentSerializer,
)

SEGMENT_MEMBERS_LIST = extend_schema(
    operation_id="admin_segment_members_list",
    parameters=[EXPORT_PARAM],
    responses={200: CustomerListItemSerializer(many=True), **ADMIN_ERRORS},
)

SEGMENT_MEMBERS_ADD = extend_schema(
    operation_id="admin_segment_members_add",
    description=(
        "409 `conflict` while an approved `scheduled` notification targets the segment (its audience "
        "must not grow before the dispatch)."
    ),
    request=SegmentMembersInputSerializer,
    responses={200: SegmentSerializer, **ADMIN_ERRORS},
)

SEGMENT_MEMBER_DETAIL_DELETE = extend_schema(
    operation_id="admin_segment_members_remove",
    description="Removes a manual membership; a rule-only member answers 409 conflict.",
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "SEGMENT_MEMBERS_ADD",
    "SEGMENT_MEMBERS_LIST",
    "SEGMENT_MEMBER_DETAIL_DELETE",
]
