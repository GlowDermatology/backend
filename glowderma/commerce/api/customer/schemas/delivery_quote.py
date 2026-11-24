"""OpenAPI declarations for ``delivery/calculate/`` (customer contract §2.6): the delivery options and price
of the cart."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.customer.serializers import (
    CustomerDeliveryCalculateSerializer,
    CustomerDeliveryQuoteSerializer,
)
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)

DELIVERY_CALCULATE_POST = extend_schema(
    operation_id="customer_delivery_calculate",
    parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=CustomerDeliveryCalculateSerializer,
    responses={200: CustomerDeliveryQuoteSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "DELIVERY_CALCULATE_POST",
]
