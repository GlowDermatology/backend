"""OpenAPI declarations for the customer's order actions (customer contract §2.7): cancel and reorder."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.customer.serializers import (
    CustomerCartSerializer,
    CustomerOrderDetailSerializer,
    CustomerReorderSerializer,
)
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
)

CANCEL_POST = extend_schema(
    operation_id="customer_orders_cancel",
    parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: CustomerOrderDetailSerializer, **CUSTOMER_ERRORS},
)

REORDER_POST = extend_schema(
    operation_id="customer_orders_reorder",
    parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=CustomerReorderSerializer,
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "CANCEL_POST",
    "REORDER_POST",
]
