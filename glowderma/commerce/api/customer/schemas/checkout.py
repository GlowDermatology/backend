"""OpenAPI declarations for checkout (customer contract §2.7): the order from the cart, its delivery quote and
the cashback."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.customer.serializers import (
    CustomerCheckoutSerializer,
    CustomerOrderDetailSerializer,
)
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
)

CHECKOUT_POST = extend_schema(
    operation_id="customer_orders_checkout",
    parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=CustomerCheckoutSerializer,
    responses={201: CustomerOrderDetailSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "CHECKOUT_POST",
]
