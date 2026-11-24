"""OpenAPI declarations for the admin specialists list (with its export), card and PATCH."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.professionals.api.admin.serializers import (
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    SpecialistListQuerySerializer,
)

SPECIALIST_LIST = extend_schema(
    operation_id="admin_specialists_list",
    parameters=[SpecialistListQuerySerializer, EXPORT_PARAM],
    responses={200: ClinicSpecialistSerializer(many=True), **ADMIN_ERRORS},
)

SPECIALIST_RETRIEVE = extend_schema(
    operation_id="admin_specialists_retrieve", responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS}
)

SPECIALIST_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_specialists_partial_update",
    description=(
        "Every field optional; texts merge leaf by leaf. `is_active: false` with requested/confirmed "
        "appointments ahead answers 409 `conflict` naming their count unless `force: true` (the "
        "appointments are kept either way)."
    ),
    request=ClinicSpecialistPatchSerializer,
    responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_LIST",
    "SPECIALIST_PARTIAL_UPDATE",
    "SPECIALIST_RETRIEVE",
]
