"""OpenAPI declarations for the dispatch suspension (Yandex Delivery plan §3.9)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.admin.serializers import (
    SuspensionBodySerializer,
    SuspensionSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)

DELIVERY_SUSPENSION_GET = extend_schema(
    operation_id="admin_delivery_suspension_retrieve",
    description="Whether courier dispatch is suspended now (`until` passed → `suspended: false`).",
    responses={200: SuspensionSerializer, **ADMIN_ERRORS},
)

DELIVERY_SUSPENSION_POST = extend_schema(
    operation_id="admin_delivery_suspension_update",
    description="Suspend courier dispatch (until `until`, or until lifted) or resume it; audited "
    "`delivery.suspension_updated`. `until` must be in the future (`in_past`); it is ignored when "
    "resuming.",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=SuspensionBodySerializer,
    responses={200: SuspensionSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "DELIVERY_SUSPENSION_GET",
    "DELIVERY_SUSPENSION_POST",
]
