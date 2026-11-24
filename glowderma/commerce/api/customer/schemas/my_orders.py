"""OpenAPI declarations for the customer's orders (customer contract §2.8): the list and the detail."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.customer.serializers import (
    CustomerOrderDetailSerializer,
    CustomerOrderScopeQuerySerializer,
    CustomerOrderSummarySerializer,
)
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
)

MY_ORDERS_LIST = extend_schema(
    operation_id="customer_orders_list",
    parameters=[*DEVICE_HEADERS, CustomerOrderScopeQuerySerializer],
    responses={200: CustomerOrderSummarySerializer(many=True), **CUSTOMER_ERRORS},
)

MY_ORDERS_RETRIEVE = extend_schema(
    operation_id="customer_orders_retrieve",
    parameters=DEVICE_HEADERS,
    responses={200: CustomerOrderDetailSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "MY_ORDERS_LIST",
    "MY_ORDERS_RETRIEVE",
]
