"""OpenAPI declarations for A specialist's free slots (O)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.appointments.api.customer.schemas.common import PUBLIC_HEADERS
from glowderma.appointments.api.customer.serializers import (
    FreeSlotsSerializer,
)
from glowderma.core.api.schema import (
    PUBLIC_CUSTOMER_ERRORS,
)

SPECIALIST_SLOTS_GET = extend_schema(
    operation_id="customer_specialist_slots_list",
    description=(
        "Free start times of one specialist for one service on one day (grid relative to the start of "
        "the working hours; lead time, blocks, the break and open appointments removed)."
    ),
    parameters=[
        *PUBLIC_HEADERS,
        OpenApiParameter("date", str, OpenApiParameter.QUERY, required=True, description="YYYY-MM-DD"),
        OpenApiParameter("service_id", int, OpenApiParameter.QUERY, required=True),
        OpenApiParameter("location_id", int, OpenApiParameter.QUERY, required=True),
    ],
    responses={200: FreeSlotsSerializer, **PUBLIC_CUSTOMER_ERRORS},
)


__all__ = [
    "SPECIALIST_SLOTS_GET",
]
