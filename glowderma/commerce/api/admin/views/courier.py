"""Dispatching an order to a courier (contract §2.2)."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.commerce import services
from glowderma.commerce.api.admin.schemas.courier import ORDER_DISPATCH_POST
from glowderma.commerce.api.admin.serializers import (
    DispatchSerializer,
)
from glowderma.commerce.api.admin.views.common import actor_id, order_detail_response, order_or_404
from glowderma.commerce.models import Order
from glowderma.core.idempotency import idempotent


class OrderDispatchView(AdminAPIView):
    """Yandex Delivery plan §3.6: staff call a courier. Nothing is sent to Yandex in the request: the claim
    is stored and ``yandex.dispatch`` enqueued (202, the order detail; ``delivery.stage`` =
    ``courier_requested``)."""

    @ORDER_DISPATCH_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = DispatchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        order_or_404(
            lambda: services.request_dispatch(
                pk,
                actor_id(request),
                weight_g=data["weight_g"],
                method=data["method"],
                approved_price=data["approved_price"],
                request=request,
            )
        )
        return order_detail_response(pk, status=202)


__all__ = [
    "OrderDispatchView",
]
