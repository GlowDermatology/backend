"""OpenAPI declarations for ``notifications/meta/``: the options of the campaign editor."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)
from glowderma.notifications.api.admin.serializers import (
    NotificationMetaSerializer,
)

NOTIFICATION_META_GET = extend_schema(
    operation_id="admin_notifications_meta", responses={200: NotificationMetaSerializer, **ADMIN_ERRORS}
)


__all__ = [
    "NOTIFICATION_META_GET",
]
