"""The specialist's own services."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.admin.serializers import (
    ClinicServiceWriteSerializer,
)
from glowderma.professionals.api.specialist.base import SpecialistProfileAPIView
from glowderma.professionals.api.specialist.payloads import own_profile_body
from glowderma.professionals.api.specialist.schemas.own_services import (
    OWN_SERVICE_DELETE,
    OWN_SERVICE_PATCH,
    OWN_SERVICES_POST,
)


class OwnServicesView(SpecialistProfileAPIView):
    @OWN_SERVICES_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        body = ClinicServiceWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.create_specialist_service(
            self.professional.pk, dict(body.validated_data), actor=request.user, request=request
        )
        return Response(own_profile_body(self.professional.pk), status=201)


class OwnServiceView(SpecialistProfileAPIView):
    @OWN_SERVICE_PATCH
    @idempotent(required=False)
    def patch(self, request: Request, pk: int) -> Response:
        body = ClinicServiceWriteSerializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        services.update_specialist_service(
            self.professional.pk, pk, dict(body.validated_data), actor=request.user, request=request
        )
        return Response(own_profile_body(self.professional.pk))

    @OWN_SERVICE_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        services.delete_specialist_service(self.professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "OwnServiceView",
    "OwnServicesView",
]
