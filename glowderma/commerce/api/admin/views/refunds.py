"""The refund operations of the payments screen (contract §7 item 18: ``payments/{id}/refunds/{refund_id}/
retry|resolve/``, declared here because they write order events)."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.commerce import services
from glowderma.commerce.api.admin.schemas.refunds import REFUND_RESOLVE_POST, REFUND_RETRY_POST
from glowderma.commerce.api.admin.serializers import (
    ResolveRefundSerializer,
)
from glowderma.commerce.api.admin.views.common import actor_id
from glowderma.core.idempotency import idempotent
from glowderma.payments import selectors as payment_selectors
from glowderma.payments.api.admin.serializers import PaymentSerializer
from glowderma.payments.models import Payment
from glowderma.payments.services import RefundNotFound


def _payment_response(payment_id: int) -> Response:
    payment = Payment.objects.select_related("order__customer").filter(pk=payment_id).first()
    if payment is None:
        raise NotFound
    refunds = payment_selectors.refunds_by_payment([payment.pk])
    return Response(PaymentSerializer(payment, context={"refunds": refunds}).data)


def _refund_of_payment(payment_id: int, refund_id: int) -> None:
    refund = payment_selectors.get_refund(refund_id)
    if refund is None or refund.payment_id != payment_id:
        raise NotFound


class RefundRetryView(AdminAPIView):
    @REFUND_RETRY_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int, refund_id: int) -> Response:
        _refund_of_payment(pk, refund_id)
        try:
            services.retry_refund(refund_id, actor_id(request), request=request)
        except RefundNotFound as exc:
            raise NotFound from exc
        return _payment_response(pk)


class RefundResolveView(AdminAPIView):
    @REFUND_RESOLVE_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int, refund_id: int) -> Response:
        _refund_of_payment(pk, refund_id)
        body = ResolveRefundSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            services.resolve_refund(
                refund_id,
                body.validated_data["state"],
                actor_id(request),
                manual_payout=bool(body.validated_data.get("manual_payout", False)),
                request=request,
            )
        except RefundNotFound as exc:
            raise NotFound from exc
        return _payment_response(pk)


__all__ = [
    "RefundResolveView",
    "RefundRetryView",
]
