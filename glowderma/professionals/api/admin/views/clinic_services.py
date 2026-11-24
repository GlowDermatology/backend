"""A specialist's own services: create, update and delete (or deactivate when booked)."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.admin.payloads import specialist_body
from glowderma.professionals.api.admin.schemas.clinic_services import (
    SPECIALIST_SERVICE_DELETE,
    SPECIALIST_SERVICE_PATCH,
    SPECIALIST_SERVICES_POST,
)
from glowderma.professionals.api.admin.serializers import (
    ClinicServiceWriteSerializer,
)
from glowderma.professionals.api.admin.views.common import professional_or_404


class SpecialistServicesView(AdminAPIView):
    @SPECIALIST_SERVICES_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        serializer = ClinicServiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.create_specialist_service(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(specialist_body(pk), status=201)


class SpecialistServiceView(AdminAPIView):
    @SPECIALIST_SERVICE_PATCH
    @idempotent(required=False)
    def patch(self, request: Request, pk: int, service_id: int) -> Response:
        professional_or_404(pk)
        serializer = ClinicServiceWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_specialist_service(
            pk, service_id, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(specialist_body(pk))

    @SPECIALIST_SERVICE_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, service_id: int) -> Response:
        professional_or_404(pk)
        services.delete_specialist_service(pk, service_id, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "SpecialistServiceView",
    "SpecialistServicesView",
]
