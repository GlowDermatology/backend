"""OpenAPI declarations for the clinic locations: the list, create and update."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.professionals.api.admin.serializers import (
    ClinicLocationCreateSerializer,
    ClinicLocationPatchSerializer,
    ClinicLocationSerializer,
    ClinicLocationsSerializer,
)

CLINICS_GET = extend_schema(
    operation_id="admin_clinics_list",
    responses={200: ClinicLocationsSerializer, **ADMIN_ERRORS},
)

CLINICS_POST = extend_schema(
    operation_id="admin_clinics_create",
    request=ClinicLocationCreateSerializer,
    responses={201: ClinicLocationSerializer, **ADMIN_ERRORS},
)

CLINIC_GET = extend_schema(
    operation_id="admin_clinics_retrieve",
    responses={200: ClinicLocationSerializer, **ADMIN_ERRORS},
)

CLINIC_PATCH = extend_schema(
    operation_id="admin_clinics_partial_update",
    description="Deactivate after reassigning active specialists and resolving upcoming visits.",
    request=ClinicLocationPatchSerializer,
    responses={200: ClinicLocationSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CLINICS_GET",
    "CLINICS_POST",
    "CLINIC_GET",
    "CLINIC_PATCH",
]
