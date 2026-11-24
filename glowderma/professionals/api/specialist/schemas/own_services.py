"""OpenAPI declarations for the specialist's own services."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.professionals.api.admin.serializers import (
    ClinicServiceWriteSerializer,
)
from glowderma.professionals.api.specialist.serializers import SpecialistOwnProfileSerializer

OWN_SERVICES_POST = extend_schema(
    operation_id="specialist_services_create",
    description=(
        "A new own service with the price and duration a booking snapshots; appended after the others. "
        "A name already used answers 400 `unique`; at most 50 services."
    ),
    request=ClinicServiceWriteSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={201: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
)

OWN_SERVICE_PATCH = extend_schema(
    operation_id="specialist_services_partial_update",
    description=(
        "Every field optional; texts merge leaf by leaf. `is_active: false` stops offering the service "
        "at once; booked appointments keep their price and duration. Another specialist's service is 404."
    ),
    request=ClinicServiceWriteSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
)

OWN_SERVICE_DELETE = extend_schema(
    operation_id="specialist_services_destroy",
    description=(
        "Delete an own service that was never booked. One with appointments (any status) answers 409 "
        "`conflict`: deactivate it instead (`is_active: false`)."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "OWN_SERVICES_POST",
    "OWN_SERVICE_DELETE",
    "OWN_SERVICE_PATCH",
]
