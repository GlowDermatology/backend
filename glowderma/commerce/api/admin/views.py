"""Admin views of `commerce`: orders (contract §2.2) and the refund operations of the payments screen
(contract §7 item 18: ``payments/{id}/refunds/{refund_id}/retry|resolve/``, declared here because they write
order events)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.commerce import selectors, services
from glowderma.commerce.api.admin.serializers import (
    MESSAGE_REASON,
    CancelOrderSerializer,
    OrderCountsQuerySerializer,
    OrderDetailSerializer,
    OrderListQuerySerializer,
    OrderSummarySerializer,
    ResolveRefundSerializer,
    StatusCountsSerializer,
    TransitionSerializer,
)
from glowderma.commerce.models import Order
from glowderma.core import labels
from glowderma.core.api.exceptions import Conflict, ContractValidationError
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
    IDEMPOTENCY_KEY_HEADER,
    ErrorEnvelopeSerializer,
)
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent
from glowderma.loyalty.services import PurchaseConflict
from glowderma.payments import selectors as payment_selectors
from glowderma.payments.api.admin.serializers import PaymentSerializer
from glowderma.payments.models import Payment
from glowderma.payments.services import RefundNotFound

ACTION_ERRORS = {**ADMIN_ERRORS, 503: ErrorEnvelopeSerializer}


def _order_or_404[T](call: Callable[[], T]) -> T:
    """Run a service call, mapping a missing order to 404 and a loyalty purchase conflict to 409."""
    try:
        return call()
    except services.OrderNotFound as exc:
        raise NotFound from exc
    except PurchaseConflict as exc:  # loyalty's text is internal English: answer with the Uzbek message
        raise Conflict(services.MESSAGE_PURCHASE_CONFLICT) from exc


def _actor_id(request: Request) -> int:
    return cast(int, request.user.pk)


def _detail_response(order_id: int, status: int = 200) -> Response:
    detail = selectors.order_detail(order_id)
    if detail is None:
        raise NotFound
    return Response(OrderDetailSerializer(detail).data, status=status)


class OrderViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = OrderSummarySerializer
    page_size_options = (6, 10, 25, 50)
    default_page_size = 10
    export_name = "buyurtmalar"
    export_resource = "orders"
    export_headers = labels.ORDER_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Order]:
        query = OrderListQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        return selectors.orders_list(cast(selectors.OrderFilters, dict(query.validated_data)))

    def get_export_rows(self, items: Iterable[Order]) -> Iterable[Sequence[Any]]:
        for order in items:
            payment = selectors.payment_summary(order)
            yield (
                order.pk,
                order.customer.name,
                order.customer.phone,
                format_export_datetime(order.created_at),
                order.total,
                labels.PAYMENT_STATUS[payment["status"]],
                labels.PROVIDER[payment["provider"]],
                labels.FULFILMENT[order.fulfilment],
                labels.ORDER_STATUS[order.status],
                selectors.tracking_number(order),
            )

    @extend_schema(
        operation_id="admin_orders_list",
        parameters=[OrderListQuerySerializer, EXPORT_PARAM],
        responses={200: OrderSummarySerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_orders_retrieve", responses={200: OrderDetailSerializer, **ADMIN_ERRORS}
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        return _detail_response(pk)


class OrderStatusCountsView(AdminAPIView):
    @extend_schema(
        operation_id="admin_orders_status_counts",
        parameters=[OrderCountsQuerySerializer],
        responses={200: StatusCountsSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        query = OrderCountsQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        counts = selectors.status_counts(cast(selectors.OrderFilters, dict(query.validated_data)))
        return Response(StatusCountsSerializer(counts).data)


class OrderCancelView(AdminAPIView):
    @extend_schema(
        operation_id="admin_orders_cancel",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=CancelOrderSerializer,
        responses={200: OrderDetailSerializer, **ACTION_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = CancelOrderSerializer(data=request.data)
        if not body.is_valid():
            raise ContractValidationError(body.errors, message=MESSAGE_REASON)
        _order_or_404(
            lambda: services.cancel_order(
                pk, body.validated_data["reason"], _actor_id(request), request=request
            )
        )
        return _detail_response(pk)


class OrderTransitionView(AdminAPIView):
    @extend_schema(
        operation_id="admin_orders_transition",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=TransitionSerializer,
        responses={200: OrderDetailSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        _order_or_404(
            lambda: services.transition(
                pk, body.validated_data["status"], _actor_id(request), request=request
            )
        )
        return _detail_response(pk)


class OrderRetryShipmentView(AdminAPIView):
    @extend_schema(
        operation_id="admin_orders_retry_shipment",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={202: OrderDetailSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        _order_or_404(lambda: services.retry_shipment(pk, _actor_id(request), request=request))
        return _detail_response(pk, status=202)


# --- refund operations (contract §7 item 18) ----------------------------------------------------------------


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
    @extend_schema(
        operation_id="admin_payments_refund_retry",
        description="Retry a `failed` refund: a new refund of the payment (Click `requested`, Payme "
        "`manual_required`) and its `refund_requested` order event.",
        request=None,
        responses={200: PaymentSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int, refund_id: int) -> Response:
        _refund_of_payment(pk, refund_id)
        try:
            services.retry_refund(refund_id, _actor_id(request), request=request)
        except RefundNotFound as exc:
            raise NotFound from exc
        return _payment_response(pk)


class RefundResolveView(AdminAPIView):
    @extend_schema(
        operation_id="admin_payments_refund_resolve",
        description="Close an `unknown` / `manual_required` refund after a manual check, or a `failed` Click "
        "refund as `succeeded` after a fresh Click status check (`manual_payout: true` when Click still "
        "reports "
        "the payment paid and staff paid the customer by hand; 503 when Click does not answer). Audited "
        "`refund.state_changed` with the actor; order event `refund_succeeded` / `refund_failed`.",
        request=ResolveRefundSerializer,
        responses={200: PaymentSerializer, **ACTION_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int, refund_id: int) -> Response:
        _refund_of_payment(pk, refund_id)
        body = ResolveRefundSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            services.resolve_refund(
                refund_id,
                body.validated_data["state"],
                _actor_id(request),
                manual_payout=bool(body.validated_data.get("manual_payout", False)),
                request=request,
            )
        except RefundNotFound as exc:
            raise NotFound from exc
        return _payment_response(pk)
