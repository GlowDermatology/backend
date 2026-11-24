"""OpenAPI declarations for the admin orders (contract §2.2): list (with its export), detail and the status
counts."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.admin.serializers import (
    OrderCountsQuerySerializer,
    OrderDetailSerializer,
    OrderListQuerySerializer,
    OrderSummarySerializer,
    StatusCountsSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
)

ORDER_LIST = extend_schema(
    operation_id="admin_orders_list",
    parameters=[OrderListQuerySerializer, EXPORT_PARAM],
    responses={200: OrderSummarySerializer(many=True), **ADMIN_ERRORS},
)

ORDER_RETRIEVE = extend_schema(
    operation_id="admin_orders_retrieve", responses={200: OrderDetailSerializer, **ADMIN_ERRORS}
)

ORDER_STATUS_COUNTS_GET = extend_schema(
    operation_id="admin_orders_status_counts",
    parameters=[OrderCountsQuerySerializer],
    responses={200: StatusCountsSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "ORDER_LIST",
    "ORDER_RETRIEVE",
    "ORDER_STATUS_COUNTS_GET",
]
