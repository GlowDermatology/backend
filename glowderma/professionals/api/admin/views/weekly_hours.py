"""A specialist's weekly working hours."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.admin.payloads import specialist_body
from glowderma.professionals.api.admin.schemas.weekly_hours import SPECIALIST_WORKING_HOURS_PUT
from glowderma.professionals.api.admin.serializers import (
    WorkingWeekSerializer,
)
from glowderma.professionals.api.admin.views.common import professional_or_404


class SpecialistWorkingHoursView(AdminAPIView):
    @SPECIALIST_WORKING_HOURS_PUT
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        serializer = WorkingWeekSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_working_week(pk, serializer.validated_data["days"], actor=request.user, request=request)
        return Response(specialist_body(pk))


__all__ = [
    "SpecialistWorkingHoursView",
]
