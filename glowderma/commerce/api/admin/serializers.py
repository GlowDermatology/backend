"""Admin serializers of `commerce` (contract §2.2 orders, §4 ``OrderEvent``; §7 item 18 refund operations)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers

from glowderma.commerce import selectors
from glowderma.commerce.models import Order, OrderEvent
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.serializers import ActorSerializer, CustomerRefSerializer
from glowderma.payments.api.admin.serializers import PeriodQuerySerializer, RefundSerializer

REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 500
MESSAGE_REASON = "Sababni kiriting."
MESSAGE_REASON_MIN = "Kamida 3 ta belgi kiriting."
MESSAGE_REASON_MAX = "500 belgidan oshmasin."

# --- shipment union (contract §2.2 table) -------------------------------------------------------------------


class ShipmentNotRequiredSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`not_required` (pickup)")


class ShipmentNotCreatedSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`not_created`")
    pending = serializers.BooleanField()


class ShipmentFailedSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`failed`")
    last_attempt_at = serializers.DateTimeField()
    attempts = serializers.IntegerField()
    pending = serializers.BooleanField()


class ShipmentCreatedSerializer(serializers.Serializer[Any]):
    state = serializers.CharField(help_text="`created`")
    tracking_number = serializers.CharField()
    tariff = serializers.IntegerField()
    updated_at = serializers.DateTimeField()
    via = serializers.ChoiceField(choices=enums.ShipmentVia.values)


SHIPMENT_SERIALIZERS: dict[enums.ShipmentState, type[serializers.Serializer[Any]]] = {
    enums.ShipmentState.NOT_REQUIRED: ShipmentNotRequiredSerializer,
    enums.ShipmentState.NOT_CREATED: ShipmentNotCreatedSerializer,
    enums.ShipmentState.FAILED: ShipmentFailedSerializer,
    enums.ShipmentState.CREATED: ShipmentCreatedSerializer,
}
SHIPMENT_SCHEMA = PolymorphicProxySerializer(
    component_name="Shipment",
    serializers={str(state.value): cls for state, cls in SHIPMENT_SERIALIZERS.items()},
    resource_type_field_name="state",
)


def render_shipment(view: dict[str, Any]) -> dict[str, Any]:
    return dict(SHIPMENT_SERIALIZERS[enums.ShipmentState(view["state"])](view).data)


# --- summary pieces -----------------------------------------------------------------------------------------


class PaymentSummarySerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=enums.PaymentStatus.values)
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)


class OrderPaymentSerializer(PaymentSummarySerializer):
    amount = serializers.IntegerField()
    transaction_id = serializers.CharField(allow_null=True)
    confirmed_at = serializers.DateTimeField(allow_null=True)


class CancellationSerializer(serializers.Serializer[Any]):
    reason = serializers.ChoiceField(choices=enums.CancellationReason.values)
    note = serializers.CharField(allow_null=True)


class OrderSummarySerializer(serializers.Serializer[Order]):
    """Contract ``OrderSummary`` (list rows and the head of the detail)."""

    id = serializers.IntegerField()
    customer = CustomerRefSerializer()
    created_at = serializers.DateTimeField()
    total = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.OrderStatus.values)
    payment = serializers.SerializerMethodField()
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    shipment = serializers.SerializerMethodField()
    cancellation = serializers.SerializerMethodField()

    @extend_schema_field(PaymentSummarySerializer)
    def get_payment(self, obj: Order) -> dict[str, Any]:
        return dict(PaymentSummarySerializer(selectors.payment_summary(obj)).data)

    @extend_schema_field(SHIPMENT_SCHEMA)
    def get_shipment(self, obj: Order) -> dict[str, Any]:
        return render_shipment(selectors.shipment_summary(obj))

    @extend_schema_field(CancellationSerializer(allow_null=True))
    def get_cancellation(self, obj: Order) -> dict[str, Any] | None:
        value = selectors.cancellation(obj)
        return None if value is None else dict(CancellationSerializer(value).data)


# --- detail -------------------------------------------------------------------------------------------------


class OrderItemSerializer(serializers.Serializer[Any]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    billz_id = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
    quantity = serializers.IntegerField()
    unit_price = serializers.IntegerField()
    promo = serializers.BooleanField()


class CashbackHoldSerializer(serializers.Serializer[Any]):
    amount = serializers.IntegerField()
    state = serializers.ChoiceField(choices=enums.CashbackHoldState.values)
    expires_at = serializers.DateTimeField()


class OrderCashbackSerializer(serializers.Serializer[Any]):
    ledger_entry_id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.OrderCashbackKind.values)
    amount = serializers.IntegerField()
    at = serializers.DateTimeField()
    base = serializers.IntegerField(allow_null=True)
    percent = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True)
    reverses_id = serializers.IntegerField(allow_null=True)


class OrderEventSerializer(serializers.Serializer[OrderEvent]):
    """Contract §4 ``OrderEvent``: ``status_after`` ``""`` → ``null``; ``actor`` for source ``admin``."""

    id = serializers.IntegerField()
    type = serializers.ChoiceField(choices=enums.OrderEventType.values)
    at = serializers.DateTimeField()
    source = serializers.ChoiceField(choices=enums.EventSource.values)  # type: ignore[assignment]
    status_after = serializers.SerializerMethodField()
    actor = ActorSerializer(allow_null=True)
    data = serializers.JSONField()  # type: ignore[assignment]  # a field, not Serializer.data

    @extend_schema_field(serializers.ChoiceField(choices=enums.OrderStatus.values, allow_null=True))
    def get_status_after(self, obj: OrderEvent) -> str | None:
        return obj.status_after or None


class AvailableActionsSerializer(serializers.Serializer[Any]):
    transitions = serializers.ListField(child=serializers.ChoiceField(choices=enums.OrderStatus.values))
    cancel = serializers.BooleanField()
    retry_shipment = serializers.BooleanField()


class OrderDetailSerializer(serializers.Serializer[selectors.OrderDetail]):
    """Contract ``Order`` = ``OrderSummary`` + the detail keys (rendered from ``selectors.order_detail``)."""

    id = serializers.IntegerField(source="order.id")
    customer = CustomerRefSerializer(source="order.customer")
    created_at = serializers.DateTimeField(source="order.created_at")
    total = serializers.IntegerField(source="order.total")
    status = serializers.ChoiceField(source="order.status", choices=enums.OrderStatus.values)
    payment = OrderPaymentSerializer()
    fulfilment = serializers.ChoiceField(source="order.fulfilment", choices=enums.FulfilmentMethod.values)
    shipment = serializers.SerializerMethodField()
    cancellation = serializers.SerializerMethodField()
    channel = serializers.ChoiceField(source="order.channel", choices=enums.OrderChannel.values)
    address = serializers.SerializerMethodField()
    items = OrderItemSerializer(many=True)
    items_subtotal = serializers.IntegerField(source="order.items_subtotal")
    promo_subtotal = serializers.IntegerField(source="order.promo_subtotal")
    delivery_fee = serializers.IntegerField(source="order.delivery_fee")
    cashback_used = serializers.IntegerField(source="order.cashback_used")
    refunds = RefundSerializer(many=True)
    cashback_hold = CashbackHoldSerializer(allow_null=True)
    cashback = OrderCashbackSerializer(many=True)
    history = OrderEventSerializer(many=True)
    available_actions = AvailableActionsSerializer()

    @extend_schema_field(SHIPMENT_SCHEMA)
    def get_shipment(self, obj: selectors.OrderDetail) -> dict[str, Any]:
        return render_shipment(obj.shipment)

    @extend_schema_field(CancellationSerializer(allow_null=True))
    def get_cancellation(self, obj: selectors.OrderDetail) -> dict[str, Any] | None:
        value = selectors.cancellation(obj.order)
        return None if value is None else dict(CancellationSerializer(value).data)

    def get_address(self, obj: selectors.OrderDetail) -> str | None:
        return obj.order.address_text or None


# --- queries and bodies -------------------------------------------------------------------------------------


class OrderCountsQuerySerializer(PeriodQuerySerializer):
    """``GET orders/status-counts/``: the ``orders/`` params except ``status``, ``ordering`` and paging."""

    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values, required=False)
    payment = serializers.ChoiceField(choices=enums.PaymentStatus.values, required=False)
    customer = serializers.IntegerField(required=False, min_value=1)


class OrderListQuerySerializer(OrderCountsQuerySerializer):
    status = serializers.ChoiceField(choices=enums.OrderStatus.values, required=False)
    ordering = serializers.ChoiceField(
        choices=["-created_at", "created_at"], required=False, default="-created_at"
    )


class StatusCountsSerializer(serializers.Serializer[dict[str, int]]):
    all = serializers.IntegerField()
    awaiting_payment = serializers.IntegerField()
    processing = serializers.IntegerField()
    handed_to_courier = serializers.IntegerField()
    delivered = serializers.IntegerField()
    cancelled = serializers.IntegerField()


class CancelOrderSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.CharField(allow_blank=True, trim_whitespace=True)

    def validate_reason(self, value: str) -> str:
        if len(value) < REASON_MIN_LENGTH:
            raise serializers.ValidationError(field_error("min_length", MESSAGE_REASON_MIN))
        if len(value) > REASON_MAX_LENGTH:
            raise serializers.ValidationError(field_error("max_length", MESSAGE_REASON_MAX))
        return value


class TransitionSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=enums.OrderStatus.values)


RESOLVE_STATES = (enums.RefundState.SUCCEEDED, enums.RefundState.FAILED)


class ResolveRefundSerializer(serializers.Serializer[dict[str, Any]]):
    """``{state: succeeded | failed, manual_payout?: bool}`` (a ``CharField`` so the schema keeps one
    ``RefundStateEnum``); ``manual_payout`` (admin §7 item 18, M2) closes a ``failed`` Click refund Click
    still
    reports ``paid`` after staff paid the customer outside Click; ignored for other refunds."""

    state = serializers.CharField(help_text="`succeeded` or `failed`")
    manual_payout = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Staff paid the customer outside Click (closing a `failed` Click refund Click reports "
        "`paid`).",
    )

    def validate_state(self, value: str) -> str:
        if value not in RESOLVE_STATES:
            raise serializers.ValidationError(field_error("invalid_choice"))
        return value
