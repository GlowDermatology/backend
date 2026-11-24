"""OpenAPI declarations for the status push templates."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)
from glowderma.notifications.api.admin.serializers import (
    StatusTemplateSerializer,
    TemplateUpdateSerializer,
)

TEMPLATE_GET = extend_schema(
    operation_id="admin_notification_templates_retrieve",
    responses={200: StatusTemplateSerializer, **ADMIN_ERRORS},
)

TEMPLATE_PUT = extend_schema(
    operation_id="admin_notification_templates_update",
    request=TemplateUpdateSerializer,
    responses={200: StatusTemplateSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "TEMPLATE_GET",
    "TEMPLATE_PUT",
]
