"""A specialist's date overrides of the weekly hours."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors, services
from glowderma.professionals.api.admin.schemas.overrides import (
    SPECIALIST_OVERRIDE_CREATE,
    SPECIALIST_OVERRIDE_DESTROY,
    SPECIALIST_OVERRIDE_LIST,
)
from glowderma.professionals.api.admin.serializers import (
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
)
from glowderma.professionals.api.admin.views.common import professional_or_404, query_params
from glowderma.professionals.models import (
    WorkingHoursOverride,
)


class SpecialistOverrideViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = HoursOverrideSerializer
    queryset = WorkingHoursOverride.objects.none()  # the schema's model; rows come from get_queryset

    def get_queryset(self) -> QuerySet[WorkingHoursOverride]:
        if getattr(self, "swagger_fake_view", False):
            return WorkingHoursOverride.objects.none()
        professional = professional_or_404(self.kwargs["pk"])
        query = ScheduleRangeQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.overrides_of(professional, **query.validated_data)

    @SPECIALIST_OVERRIDE_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @SPECIALIST_OVERRIDE_CREATE
    @idempotent(required=False)
    def create(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        serializer = HoursOverrideCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.create_override(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(HoursOverrideSerializer(row).data, status=201)

    @SPECIALIST_OVERRIDE_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int, override_id: int) -> Response:
        professional_or_404(pk)
        services.delete_override(pk, override_id, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "SpecialistOverrideViewSet",
]
