"""OpenAPI declarations shared by the admin and specialist ``appointments`` views: the action path
parameter."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter

from glowderma.core import enums

ACTION_PARAM = OpenApiParameter(
    name="action",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.PATH,
    enum=list(enums.AppointmentAction.values),
)


__all__ = [
    "ACTION_PARAM",
]
