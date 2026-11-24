"""OpenAPI declarations for A specialist's own services: create, update and delete (or deactivate when
booked)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.professionals.api.admin.serializers import (
    ClinicServiceWriteSerializer,
    ClinicSpecialistSerializer,
)

SPECIALIST_SERVICES_POST = extend_schema(
    operation_id="admin_specialists_services_create",
    description=(
        "A new service of this specialist with the price and duration a booking snapshots; appended "
        "after its others. A name the specialist already uses answers 400 `unique`; at most 50 services."
    ),
    request=ClinicServiceWriteSerializer,
    responses={201: ClinicSpecialistSerializer, **ADMIN_ERRORS},
)

SPECIALIST_SERVICE_PATCH = extend_schema(
    operation_id="admin_specialists_services_partial_update",
    description=(
        "Every field optional; texts merge leaf by leaf. `is_active: false` stops offering the service "
        "at once; booked appointments keep their price and duration. Another specialist's service is 404."
    ),
    request=ClinicServiceWriteSerializer,
    responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
)

SPECIALIST_SERVICE_DELETE = extend_schema(
    operation_id="admin_specialists_services_destroy",
    description=(
        "Delete a service that was never booked. One with appointments (any status) answers 409 "
        "`conflict`: deactivate it instead (`is_active: false`)."
    ),
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_SERVICES_POST",
    "SPECIALIST_SERVICE_DELETE",
    "SPECIALIST_SERVICE_PATCH",
]
