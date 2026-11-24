"""Customer segments: list (with its export), create, update and delete."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.core import labels
from glowderma.core.exports import ExportMixin
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.notifications import selectors, services
from glowderma.notifications.api.admin.schemas.segments import (
    SEGMENT_CREATE,
    SEGMENT_DESTROY,
    SEGMENT_LIST,
    SEGMENT_PARTIAL_UPDATE,
    SEGMENT_RETRIEVE,
)
from glowderma.notifications.api.admin.serializers import (
    SegmentSerializer,
    SegmentWriteSerializer,
)
from glowderma.notifications.api.admin.views.common import segment_or_404
from glowderma.notifications.models import CustomerSegment


class SegmentViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = SegmentSerializer
    export_name = N_("guruhlar")
    export_filter_keys = frozenset[str]()  # no filters
    export_resource = "segments"
    export_headers = labels.SEGMENT_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[CustomerSegment]:
        return CustomerSegment.objects.order_by("id")

    def get_export_rows(self, items: Iterable[CustomerSegment]) -> Iterable[Sequence[Any]]:
        for segment in items:
            stats = selectors.segment_stats(segment)
            yield (segment.pk, segment.name, stats["member_count"], stats["size"])

    @SEGMENT_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @SEGMENT_CREATE
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = SegmentWriteSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        rules = serializer.validated_data.get("rules", {})
        segment = services.create_segment(
            name=serializer.validated_data["name"],
            min_online_orders=rules.get("min_online_orders"),
            min_lifetime_amount=rules.get("min_lifetime_amount"),
            actor=request.user,
            request=request,
        )
        return Response(SegmentSerializer(segment).data, status=201)

    @SEGMENT_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(SegmentSerializer(segment_or_404(pk)).data)

    @SEGMENT_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        segment = segment_or_404(pk)
        serializer = SegmentWriteSerializer(
            data=cast(dict[str, Any], request.data), instance=segment, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated = services.update_segment(
            segment, serializer.to_changes(), actor=request.user, request=request
        )
        return Response(SegmentSerializer(updated).data)

    @SEGMENT_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int) -> Response:
        services.delete_segment(segment_or_404(pk), actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "SegmentViewSet",
]
