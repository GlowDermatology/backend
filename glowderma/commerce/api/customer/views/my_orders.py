"""The customer's orders (customer contract §2.8): the list and the detail."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerGenericViewSet
from glowderma.commerce import selectors
from glowderma.commerce.api.customer.schemas.my_orders import MY_ORDERS_LIST, MY_ORDERS_RETRIEVE
from glowderma.commerce.api.customer.serializers import (
    CustomerOrderScopeQuerySerializer,
    CustomerOrderSummarySerializer,
)
from glowderma.commerce.api.customer.views.common import order_detail_response, own_order, signed_in_customer
from glowderma.core import timeutils


class MyOrdersViewSet(CustomerGenericViewSet):
    page_size_options = (10, 20)
    default_page_size = 10

    def _now(self) -> datetime:
        return timeutils.now()

    @MY_ORDERS_LIST
    def list(self, request: Request) -> Response:
        query = CustomerOrderScopeQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        orders = selectors.customer_orders(signed_in_customer(request).pk, query.validated_data["scope"])
        page = self.paginate_queryset(orders)
        now = self._now()
        rows = [selectors.order_summary(order, self.language, now) for order in page or []]
        serializer: Any = CustomerOrderSummarySerializer(cast(Any, rows), many=True)
        return self.get_paginated_response(serializer.data)

    @MY_ORDERS_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        return order_detail_response(own_order(signed_in_customer(request), int(pk)), self.language)


__all__ = [
    "MyOrdersViewSet",
]
