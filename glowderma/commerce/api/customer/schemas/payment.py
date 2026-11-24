"""OpenAPI declarations for pay (customer contract §2.7): start a payment of the order with the chosen
provider."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.customer.serializers import (
    CustomerPayLinkSerializer,
    CustomerPaySerializer,
)
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
)

PAY_POST = extend_schema(
    operation_id="customer_orders_pay",
    parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=CustomerPaySerializer,
    responses={200: CustomerPayLinkSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "PAY_POST",
]
