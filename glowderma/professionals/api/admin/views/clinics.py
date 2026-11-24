"""The clinic locations: the list, create and update."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.admin.schemas.clinics import (
    CLINIC_GET,
    CLINIC_PATCH,
    CLINICS_GET,
    CLINICS_POST,
)
from glowderma.professionals.api.admin.serializers import (
    ClinicLocationCreateSerializer,
    ClinicLocationPatchSerializer,
    ClinicLocationSerializer,
    ClinicLocationsSerializer,
)
from glowderma.professionals.models import (
    Location,
)


class ClinicsView(AdminAPIView):
    @CLINICS_GET
    def get(self, request: Request) -> Response:
        return Response(ClinicLocationsSerializer({"results": Location.objects.all()}).data)

    @CLINICS_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        serializer = ClinicLocationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        location = services.create_location(
            dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(ClinicLocationSerializer(location).data, status=201)


class ClinicView(AdminAPIView):
    @CLINIC_GET
    def get(self, request: Request, pk: int) -> Response:
        location = Location.objects.filter(pk=pk).first()
        if location is None:
            raise NotFound
        return Response(ClinicLocationSerializer(location).data)

    @CLINIC_PATCH
    @idempotent(required=False)
    def patch(self, request: Request, pk: int) -> Response:
        serializer = ClinicLocationPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        location = services.update_location(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(ClinicLocationSerializer(location).data)


__all__ = [
    "ClinicView",
    "ClinicsView",
]
