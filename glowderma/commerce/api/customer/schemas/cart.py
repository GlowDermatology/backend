"""OpenAPI declarations for the server cart (customer contract §2.5): read, add, change and remove items."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.commerce.api.customer.serializers import (
    CustomerCartAddSerializer,
    CustomerCartQuantitySerializer,
    CustomerCartSerializer,
)
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)

PRODUCT_ID_PARAM = OpenApiParameter(
    name="product_id",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    many=True,
    required=True,
    description="1..50 product ids (repeat the parameter).",
)

CART_GET = extend_schema(
    operation_id="customer_cart_retrieve",
    parameters=DEVICE_HEADERS,
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)

CART_DELETE = extend_schema(
    operation_id="customer_cart_clear",
    parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)

CART_ITEMS_POST = extend_schema(
    operation_id="customer_cart_items_create",
    parameters=[*DEVICE_HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=CustomerCartAddSerializer,
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)

CART_ITEMS_DELETE = extend_schema(
    operation_id="customer_cart_items_bulk_delete",
    parameters=[*DEVICE_HEADERS, PRODUCT_ID_PARAM, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)

CART_ITEM_PATCH = extend_schema(
    operation_id="customer_cart_items_update",
    parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=CustomerCartQuantitySerializer,
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)

CART_ITEM_DELETE = extend_schema(
    operation_id="customer_cart_items_destroy",
    parameters=[*DEVICE_HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    responses={200: CustomerCartSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "CART_DELETE",
    "CART_GET",
    "CART_ITEMS_DELETE",
    "CART_ITEMS_POST",
    "CART_ITEM_DELETE",
    "CART_ITEM_PATCH",
    "PRODUCT_ID_PARAM",
]
