"""OpenAPI declarations for the specialist's own schedule conflicts."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.specialist.serializers import SpecialistConflictsSerializer
from glowderma.core.api.schema import ADMIN_ERRORS

CONFLICTS_GET = extend_schema(
    operation_id="specialist_conflicts",
    description=(
        "The caller's open future appointments their current schedule no longer covers, by start; "
        "reschedule or cancel them through specialist/appointments/{id}/reschedule|cancel/."
    ),
    responses={200: SpecialistConflictsSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CONFLICTS_GET",
]
