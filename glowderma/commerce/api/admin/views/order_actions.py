"""The admin order actions (contract §2.2): cancel, transition, retry the shipment and confirm a return."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.commerce import services
from glowderma.commerce.api.admin.schemas.order_actions import (
    ORDER_CANCEL_POST,
    ORDER_CONFIRM_RETURN_POST,
    ORDER_RETRY_SHIPMENT_POST,
    ORDER_TRANSITION_POST,
)
from glowderma.commerce.api.admin.serializers import (
    MESSAGE_REASON,
    CancelOrderSerializer,
    ConfirmReturnSerializer,
    TransitionSerializer,
)
from glowderma.commerce.api.admin.views.common import actor_id, order_detail_response, order_or_404
from glowderma.commerce.models import Order
from glowderma.core.api.exceptions import ContractValidationError
from glowderma.core.idempotency import already_answered, idempotency_key, idempotent


class OrderCancelView(AdminAPIView):
    @ORDER_CANCEL_POST
    def post(self, request: Request, pk: int) -> Response:
        # plan §3.8: the carrier is asked here, before the @idempotent transaction (a replay asks nobody)
        body = CancelOrderSerializer(data=request.data)
        if (
            body.is_valid()
            and idempotency_key(request) is not None
            and not already_answered(request)
            and Order.objects.filter(pk=pk).exists()
        ):
            order_or_404(lambda: services.cancel_at_carrier(pk, actor_id(request), request=request))
        return self._cancel(request, pk)

    @idempotent()
    def _cancel(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = CancelOrderSerializer(data=request.data)
        if not body.is_valid():
            raise ContractValidationError(body.errors, message=MESSAGE_REASON)
        order_or_404(
            lambda: services.cancel_order(
                pk, body.validated_data["reason"], actor_id(request), request=request
            )
        )
        return order_detail_response(pk)


class OrderTransitionView(AdminAPIView):
    @ORDER_TRANSITION_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        order_or_404(
            lambda: services.transition(pk, body.validated_data["status"], actor_id(request), request=request)
        )
        return order_detail_response(pk)


class OrderRetryShipmentView(AdminAPIView):
    @ORDER_RETRY_SHIPMENT_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        order_or_404(lambda: services.retry_shipment(pk, actor_id(request), request=request))
        return order_detail_response(pk, status=202)


class OrderConfirmReturnView(AdminAPIView):
    """Yandex Delivery plan §3.8: staff confirm the returning / returned parcel is physically back."""

    @ORDER_CONFIRM_RETURN_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = ConfirmReturnSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        order_or_404(
            lambda: services.confirm_return(
                pk, actor_id(request), note=body.validated_data["note"], request=request
            )
        )
        return order_detail_response(pk)


__all__ = [
    "OrderCancelView",
    "OrderConfirmReturnView",
    "OrderRetryShipmentView",
    "OrderTransitionView",
]
