"""OpenAPI declarations for the admin campaigns: list (with its export), retrieve, create, update and
delete."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
    IDEMPOTENCY_KEY_HEADER,
)
from glowderma.notifications.api.admin.schemas.common import (
    NotificationFormSerializer,
    NotificationPatchFormSerializer,
)
from glowderma.notifications.api.admin.serializers import (
    NotificationListQuerySerializer,
    NotificationRecordSerializer,
)

NOTIFICATION_LIST = extend_schema(
    operation_id="admin_notifications_list",
    parameters=[NotificationListQuerySerializer, EXPORT_PARAM],
    responses={200: NotificationRecordSerializer(many=True), **ADMIN_ERRORS},
)

NOTIFICATION_CREATE = extend_schema(
    operation_id="admin_notifications_create",
    description=(
        "Multipart with dotted keys (`link.kind`, `audience.type`, …) and an optional `image` (JPEG, PNG "
        "or WebP, ≤ 5 MiB). Creates a `draft` (revision 1): nothing is sent until a superuser tested, "
        "confirmed and approved it. `scheduled_at` is the requested send time the dispatch waits for."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request={"multipart/form-data": NotificationFormSerializer},
    responses={201: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_RETRIEVE = extend_schema(
    operation_id="admin_notifications_retrieve",
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_notifications_partial_update",
    description=(
        "Author or superuser; `draft` / `tested` only (409 `invalid_transition`). Multipart like the "
        "create form with every content key optional, `expected_revision` (409 `conflict` when the "
        "campaign changed meanwhile) and `remove_image`. A content change bumps `revision`, clears the "
        "test confirmation and puts a `tested` campaign back to `draft`; a `scheduled_at` change alone "
        "keeps the state (`in_past` applies to a submitted `scheduled_at` only: a kept requested time "
        "that went by does not block the edit)."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request={"multipart/form-data": NotificationPatchFormSerializer},
    responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
)

NOTIFICATION_DESTROY = extend_schema(
    operation_id="admin_notifications_destroy",
    description="Author or superuser; `draft` / `tested` only (409 `invalid_transition`).",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "NOTIFICATION_CREATE",
    "NOTIFICATION_DESTROY",
    "NOTIFICATION_LIST",
    "NOTIFICATION_PARTIAL_UPDATE",
    "NOTIFICATION_RETRIEVE",
]
