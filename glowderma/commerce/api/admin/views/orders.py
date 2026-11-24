"""The admin orders (contract §2.2): list (with its export), detail and the status counts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.commerce import delivery_admin, selectors
from glowderma.commerce.api.admin.schemas.orders import ORDER_LIST, ORDER_RETRIEVE, ORDER_STATUS_COUNTS_GET
from glowderma.commerce.api.admin.serializers import (
    OrderCountsQuerySerializer,
    OrderListQuerySerializer,
    OrderSummarySerializer,
    StatusCountsSerializer,
)
from glowderma.commerce.api.admin.views.common import order_detail_response
from glowderma.commerce.models import Order
from glowderma.core import labels
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.uzsource import N_


class OrderViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = OrderSummarySerializer
    page_size_options = (6, 10, 25, 50)
    default_page_size = 10
    export_name = N_("buyurtmalar")
    export_resource = "orders"
    export_headers = labels.ORDER_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Order]:
        query = OrderListQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        filters = cast(selectors.OrderFilters, dict(query.validated_data))
        return delivery_admin.with_delivery(selectors.orders_list(filters))

    def get_export_rows(self, items: Iterable[Order]) -> Iterable[Sequence[Any]]:
        for order in items:
            payment = selectors.payment_summary(order)
            yield (
                order.pk,
                order.customer.name,
                order.customer.phone or "",
                format_export_datetime(order.created_at),
                order.total,
                labels.text(labels.PAYMENT_STATUS, payment["status"]),
                labels.text(labels.PROVIDER, payment["provider"]),
                labels.text(labels.FULFILMENT, order.fulfilment),
                labels.text(labels.ORDER_STATUS, order.status),
                selectors.tracking_number(order),
            )

    @ORDER_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @ORDER_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        return order_detail_response(pk)


class OrderStatusCountsView(AdminAPIView):
    @ORDER_STATUS_COUNTS_GET
    def get(self, request: Request) -> Response:
        query = OrderCountsQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        counts = selectors.status_counts(cast(selectors.OrderFilters, dict(query.validated_data)))
        return Response(StatusCountsSerializer(counts).data)


__all__ = [
    "OrderStatusCountsView",
    "OrderViewSet",
]
