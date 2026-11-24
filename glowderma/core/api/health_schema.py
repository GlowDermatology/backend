"""OpenAPI declarations for"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.core.api.health_serializers import HealthSerializer

HEALTH_GET = extend_schema(
    operation_id="health_retrieve",
    auth=[],
    parameters=[OpenApiParameter("ready", OpenApiTypes.BOOL)],
    responses={200: HealthSerializer, 503: HealthSerializer},
)


__all__ = [
    "HEALTH_GET",
]
