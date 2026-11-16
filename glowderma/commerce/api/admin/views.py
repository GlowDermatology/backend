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
from glowderma.commerce import delivery_admin, selectors, services
from glowderma.commerce.api.admin.serializers import (
    MESSAGE_REASON,
    CancelOrderSerializer,
    ConfirmReturnSerializer,
    DeliveryIssueQuerySerializer,
    DeliveryIssueSerializer,
    DispatchSerializer,
    OpenIssueSerializer,
    OrderCountsQuerySerializer,
    OrderDetailSerializer,
    OrderListQuerySerializer,
    OrderSummarySerializer,
    ResolveIssueSerializer,
    ResolveRefundSerializer,
    StatusCountsSerializer,
    SuspensionBodySerializer,
    SuspensionSerializer,
    TransitionSerializer,
)
from glowderma.commerce.models import Order
from glowderma.core import enums, labels
from glowderma.core.api.exceptions import Conflict, ContractValidationError
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    EXPORT_PARAM,
    IDEMPOTENCY_KEY_HEADER,
    ErrorEnvelopeSerializer,
)
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import already_answered, idempotency_key, idempotent
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import DeliveryIssue
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
        description="Cancel an order (full refund when paid). A Yandex Delivery order is first made "
        "carrier-safe outside the transaction (plan §3.8): an open courier claim is cancelled at Yandex "
        "before pickup (free or paid), after pickup a return to the warehouse is requested and the cancel "
        "proceeds once Yandex reports it `returning`; a returning / returned parcel needs no call, a "
        "delivered order is refused. 409 `not_cancellable` (Yandex refused, delivered, parcel not coming "
        "back), 409 `return_requested` (return asked, not confirmed yet: retry later; the order is "
        "unchanged), 409 `conflict` (another Yandex operation of the order is running, or Yandex has not "
        "shown the cancel yet), 503 `provider_unavailable` (Yandex did not answer; nothing changed).",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=CancelOrderSerializer,
        responses={200: OrderDetailSerializer, **ACTION_ERRORS},
    )
    def post(self, request: Request, pk: int) -> Response:
        # plan §3.8: the carrier is asked here, before the @idempotent transaction (a replay asks nobody)
        body = CancelOrderSerializer(data=request.data)
        if (
            body.is_valid()
            and idempotency_key(request) is not None
            and not already_answered(request)
            and Order.objects.filter(pk=pk).exists()
        ):
            _order_or_404(lambda: services.cancel_at_carrier(pk, _actor_id(request), request=request))
        return self._cancel(request, pk)

    @idempotent()
    def _cancel(self, request: Request, pk: int) -> Response:
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


class OrderDispatchView(AdminAPIView):
    """Yandex Delivery plan §3.6: staff call a courier. Nothing is sent to Yandex in the request: the claim
    is stored and ``yandex.dispatch`` enqueued (202, the order detail; ``delivery.stage`` =
    ``courier_requested``)."""

    @extend_schema(
        operation_id="admin_orders_dispatch",
        description="Call a Yandex courier for a paid delivery order (plan §3.6): staff confirm they checked "
        "the contents and give the parcel weight; `approved_price` accepts the offer of an open price review "
        "(`dispatch_review`), `method` switches Standard / Express. The parcel and a new courier claim are "
        "stored and the dispatch is queued — nothing is sent to Yandex in the request (202, "
        "`delivery.stage` = `courier_requested`); the order's open `dispatch_failed` / `dispatch_review` / "
        "`preparation_overdue` issues are resolved. 409 `invalid_transition` (not a paid, open Yandex "
        "delivery waiting for a courier: stage `awaiting_dispatch` / `failed` / `returned`, a "
        "`handed_to_courier` order only once its parcel is `returned`), 409 `conflict` (a courier claim is "
        "still open — a dispatch is running or a courier may come —, a cancel is in progress, "
        "`approved_price` without an open price review, or the order is busy), 409 `coordinates_required` "
        "(the address has no map pin), 409 `method_disabled` (the method switched to is turned off), 409 "
        "`too_heavy` (the weight is above the method's class limit: courier 10 kg, express 20 kg), 409 "
        "`not_configured` (the warehouse data is incomplete), 409 `suspended` (dispatch is suspended), 409 "
        "`closed` (the warehouse is closed now), 409 `after_cutoff` (past today's dispatch deadline), 409 "
        "`returns_unavailable` (the warehouse does not take returned parcels now).",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=DispatchSerializer,
        responses={202: OrderDetailSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = DispatchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        _order_or_404(
            lambda: services.request_dispatch(
                pk,
                _actor_id(request),
                weight_g=data["weight_g"],
                method=data["method"],
                approved_price=data["approved_price"],
                request=request,
            )
        )
        return _detail_response(pk, status=202)


class OrderConfirmReturnView(AdminAPIView):
    """Yandex Delivery plan §3.8: staff confirm the returning / returned parcel is physically back."""

    @extend_schema(
        operation_id="admin_orders_confirm_return",
        description="Confirm that the parcel of a Yandex order whose delivery stage is `returning` / "
        "`returned` is physically back at the warehouse: stage `returned`, `returned_at`, the open "
        "`return_pending` issue resolved (the note is its resolution note), order event "
        "`shipment_returned`, audit `order.return_confirmed`. A cancelled order stays cancelled; an open "
        "order stays as it is (staff dispatch again or cancel). 409 `invalid_transition` (no returning "
        "parcel), 409 `conflict` (confirmed already, or the order is busy).",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=ConfirmReturnSerializer,
        responses={200: OrderDetailSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not Order.objects.filter(pk=pk).exists():
            raise NotFound
        body = ConfirmReturnSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        _order_or_404(
            lambda: services.confirm_return(
                pk, _actor_id(request), note=body.validated_data["note"], request=request
            )
        )
        return _detail_response(pk)


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


# --- delivery exception queue and dispatch suspension (Yandex Delivery plan §3.9) ---------------------------

MESSAGE_PICKUP_ISSUE = "Olib ketiladigan buyurtma uchun yetkazish muammosi ochilmaydi."
MESSAGE_ISSUE_RESOLVED = "Bu muammo allaqachon hal qilingan."


def _issue_response(issue_id: int, status: int = 200) -> Response:
    row = DeliveryIssue.objects.select_related("resolved_by").filter(pk=issue_id).first()
    if row is None:
        raise NotFound
    return Response(DeliveryIssueSerializer(row).data, status=status)


class DeliveryIssueViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = DeliveryIssueSerializer

    def get_queryset(self) -> QuerySet[DeliveryIssue]:
        query = DeliveryIssueQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        return delivery_selectors.issues_queryset(
            cast(delivery_selectors.IssueFilters, dict(query.validated_data))
        )

    @extend_schema(
        operation_id="admin_delivery_issues_list",
        description="The delivery exception queue: open issues first (urgent first, then oldest first); "
        "`state=resolved` lists the newest resolved first.",
        parameters=[DeliveryIssueQuerySerializer],
        responses={200: DeliveryIssueSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)


class OrderIssueCreateView(AdminAPIView):
    @extend_schema(
        operation_id="admin_orders_issues_create",
        description="Staff report a delivery incident of a delivery order (a wrong or missing product…): "
        "`kind` must be `incident`. 201 opens the order's incident issue; 200 refreshes the one already "
        "open (note and urgency). Audited `delivery.issue_opened`; an urgent incident alerts staff. A pickup "
        "order → 409 `conflict`.",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=OpenIssueSerializer,
        responses={201: DeliveryIssueSerializer, 200: DeliveryIssueSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        # the Order lock first (lock order Order → …), inside the idempotent transaction
        fulfilment = (
            Order.objects.select_for_update().filter(pk=pk).values_list("fulfilment", flat=True).first()
        )
        if fulfilment is None:
            raise NotFound
        body = OpenIssueSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if fulfilment != enums.FulfilmentMethod.DELIVERY:
            raise Conflict(MESSAGE_PICKUP_ISSUE)
        issue_id, created = delivery_services.open_incident(
            pk,
            note=body.validated_data["note"],
            urgent=bool(body.validated_data["urgent"]),
            actor_id=_actor_id(request),
            request=request,
        )
        return _issue_response(issue_id, status=201 if created else 200)


class DeliveryIssueResolveView(AdminAPIView):
    @extend_schema(
        operation_id="admin_delivery_issues_resolve",
        description="Close an open issue with a note (audited `delivery.issue_resolved`); 409 `conflict` "
        "when it is resolved already.",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=ResolveIssueSerializer,
        responses={200: DeliveryIssueSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not DeliveryIssue.objects.filter(pk=pk).exists():
            raise NotFound
        body = ResolveIssueSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            changed = delivery_services.resolve_issue(
                pk, actor_id=_actor_id(request), note=body.validated_data["note"], request=request
            )
        except delivery_services.IssueNotFound as exc:
            raise NotFound from exc
        if not changed:
            raise Conflict(MESSAGE_ISSUE_RESOLVED)
        return _issue_response(pk)


class DeliverySuspensionView(AdminAPIView):
    @extend_schema(
        operation_id="admin_delivery_suspension_retrieve",
        description="Whether courier dispatch is suspended now (`until` passed → `suspended: false`).",
        responses={200: SuspensionSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        current = delivery_services.suspension_of(delivery_selectors.settings_row())
        return Response(SuspensionSerializer(current).data)

    @extend_schema(
        operation_id="admin_delivery_suspension_update",
        description="Suspend courier dispatch (until `until`, or until lifted) or resume it; audited "
        "`delivery.suspension_updated`. `until` must be in the future (`in_past`); it is ignored when "
        "resuming.",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=SuspensionBodySerializer,
        responses={200: SuspensionSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        body = SuspensionBodySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        current = delivery_services.set_suspension(
            bool(body.validated_data["suspended"]),
            body.validated_data["until"],
            body.validated_data["note"],
            actor=request.user,
            request=request,
        )
        return Response(SuspensionSerializer(current).data)
