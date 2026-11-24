"""OpenAPI declarations for A specialist's weekly working hours."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.professionals.api.admin.serializers import (
    ClinicSpecialistSerializer,
    WorkingWeekSerializer,
)

SPECIALIST_WORKING_HOURS_PUT = extend_schema(
    operation_id="admin_specialists_working_hours_update",
    description=(
        "The whole week: a weekday left out is not a working day. Appointments already booked outside "
        "the new hours are kept (see `specialists/{id}/conflicts/`)."
    ),
    request=WorkingWeekSerializer,
    responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_WORKING_HOURS_PUT",
]
