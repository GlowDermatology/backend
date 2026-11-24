"""OpenAPI declarations for the staff test devices: list, register and remove."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)
from glowderma.notifications.api.admin.serializers import (
    TestDeviceCreateSerializer,
    TestDeviceSerializer,
)

TEST_DEVICE_LIST = extend_schema(
    operation_id="admin_notification_test_devices_list",
    responses={200: TestDeviceSerializer(many=True), **ADMIN_ERRORS},
)

TEST_DEVICE_CREATE = extend_schema(
    operation_id="admin_notification_test_devices_create",
    description=(
        "Superuser. Registers the most recently active pushable device of the active customer with "
        "`phone` under `label`. Field errors `phone`: `customer_not_found`, `device_not_pushable`, "
        "`unique` (already registered)."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=TestDeviceCreateSerializer,
    responses={201: TestDeviceSerializer, **ADMIN_ERRORS},
)

TEST_DEVICE_DESTROY = extend_schema(
    operation_id="admin_notification_test_devices_destroy",
    description="Superuser. Removes the device from the registry (soft, idempotent); unknown id → 404.",
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={204: None, **ADMIN_ERRORS},
)


__all__ = [
    "TEST_DEVICE_CREATE",
    "TEST_DEVICE_DESTROY",
    "TEST_DEVICE_LIST",
]
