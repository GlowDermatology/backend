"""OpenAPI declarations for the booking catalogue (O): specialists (list, detail, by slug) and services."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.customer.schemas.common import PUBLIC_HEADERS
from glowderma.appointments.api.customer.serializers import (
    ServiceListSerializer,
    SpecialistListSerializer,
    SpecialistSerializer,
)
from glowderma.core.api.schema import (
    PUBLIC_CUSTOMER_ERRORS,
)

SPECIALIST_LIST_GET = extend_schema(
    operation_id="customer_specialists_list",
    description="Active specialists by position and name, with their active services.",
    parameters=PUBLIC_HEADERS,
    responses={200: SpecialistListSerializer, **PUBLIC_CUSTOMER_ERRORS},
)

SPECIALIST_DETAIL_GET = extend_schema(
    operation_id="customer_specialists_retrieve",
    description="One active specialist (404 when inactive or unknown).",
    parameters=PUBLIC_HEADERS,
    responses={200: SpecialistSerializer, **PUBLIC_CUSTOMER_ERRORS},
)

SPECIALIST_BY_SLUG_GET = extend_schema(
    operation_id="customer_specialists_by_slug_retrieve",
    description="One active specialist by its website slug (B-W1; 404 when inactive or unknown).",
    parameters=PUBLIC_HEADERS,
    responses={200: SpecialistSerializer, **PUBLIC_CUSTOMER_ERRORS},
)

SERVICE_LIST_GET = extend_schema(
    operation_id="customer_services_list",
    description="Active services offered by at least one active specialist, with their price range.",
    parameters=PUBLIC_HEADERS,
    responses={200: ServiceListSerializer, **PUBLIC_CUSTOMER_ERRORS},
)


__all__ = [
    "SERVICE_LIST_GET",
    "SPECIALIST_BY_SLUG_GET",
    "SPECIALIST_DETAIL_GET",
    "SPECIALIST_LIST_GET",
]
