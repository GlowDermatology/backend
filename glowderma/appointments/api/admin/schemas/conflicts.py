"""OpenAPI declarations for A specialist's schedule conflicts (``specialists/{id}/conflicts/``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.admin.serializers import (
    ScheduleConflictsSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS

SPECIALIST_CONFLICTS_GET = extend_schema(
    operation_id="admin_specialists_conflicts",
    description=(
        "The specialist's requested/confirmed appointments starting now or later that its current "
        "schedule no longer covers (a day without hours, outside that day's hours, inside a blocked "
        "period or the recurring break), by start. Schedule changes never move or cancel appointments; "
        "reschedule or cancel these through appointments/{id}/reschedule|cancel/."
    ),
    responses={200: ScheduleConflictsSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_CONFLICTS_GET",
]
