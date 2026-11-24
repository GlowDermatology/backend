"""The customer's order actions (customer contract §2.7): cancel and reorder."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.commerce import services
from glowderma.commerce.api.customer.schemas.order_actions import CANCEL_POST, REORDER_POST
from glowderma.commerce.api.customer.serializers import (
    CustomerReorderSerializer,
)
from glowderma.commerce.api.customer.views.common import (
    cart_response,
    order_detail_response,
    own_order,
    signed_in_customer,
)
from glowderma.core.idempotency import idempotent


class CancelView(CustomerAPIView):
    @CANCEL_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = signed_in_customer(request)
        own_order(customer, pk)
        try:
            services.cancel_by_customer(pk, customer.pk)
        except services.OrderNotFound as exc:
            raise NotFound from exc
        return order_detail_response(own_order(customer, pk), self.language)


class ReorderView(CustomerAPIView):
    @REORDER_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = signed_in_customer(request)
        own_order(customer, pk)
        body = CustomerReorderSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if body.validated_data["cancel_unpaid"]:
            services.restore_checkout_to_cart(pk, customer.pk)
        else:
            services.reorder_into_cart(pk, customer.pk)
        return cart_response(self)


__all__ = [
    "CancelView",
    "ReorderView",
]
