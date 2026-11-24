"""A specialist's blocked periods."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors, services
from glowderma.professionals.api.admin.schemas.blocks import (
    SPECIALIST_BLOCK_CREATE,
    SPECIALIST_BLOCK_DESTROY,
    SPECIALIST_BLOCK_LIST,
)
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    ScheduleRangeQuerySerializer,
)
from glowderma.professionals.api.admin.views.common import professional_or_404, query_params
from glowderma.professionals.models import (
    BlockedPeriod,
)


class SpecialistBlockViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = BlockedPeriodSerializer
    queryset = BlockedPeriod.objects.none()  # the schema's model; rows come from get_queryset

    def get_queryset(self) -> QuerySet[BlockedPeriod]:
        if getattr(self, "swagger_fake_view", False):
            return BlockedPeriod.objects.none()
        professional = professional_or_404(self.kwargs["pk"])
        query = ScheduleRangeQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.blocks_of(professional, **query.validated_data)

    @SPECIALIST_BLOCK_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @SPECIALIST_BLOCK_CREATE
    @idempotent(required=False)
    def create(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        serializer = BlockedPeriodCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        block = services.create_block(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(BlockedPeriodSerializer(block).data, status=201)

    @SPECIALIST_BLOCK_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int, block_id: int) -> Response:
        professional_or_404(pk)
        services.delete_block(pk, block_id, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "SpecialistBlockViewSet",
]
