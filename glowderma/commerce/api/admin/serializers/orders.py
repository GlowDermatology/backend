"""The admin order: the summary row and the detail with its items, cashback, events and actions."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.commerce import delivery_admin, selectors
from glowderma.commerce.api.admin.serializers.delivery import OrderDeliverySerializer, render_delivery
from glowderma.commerce.api.admin.serializers.shipment import SHIPMENT_SCHEMA, render_shipment
from glowderma.commerce.models import Order, OrderEvent
from glowderma.core import enums
from glowderma.core.api.serializers import ActorSerializer, CustomerRefSerializer
from glowderma.payments.api.admin.serializers import RefundSerializer


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
    delivery = serializers.SerializerMethodField()

    @extend_schema_field(PaymentSummarySerializer)
    def get_payment(self, obj: Order) -> dict[str, Any]:
        return dict(PaymentSummarySerializer(selectors.payment_summary(obj)).data)

    @extend_schema_field(SHIPMENT_SCHEMA)
    def get_shipment(self, obj: Order) -> dict[str, Any]:
        return render_shipment(selectors.shipment_summary(obj))

    @extend_schema_field(OrderDeliverySerializer(allow_null=True))
    def get_delivery(self, obj: Order) -> dict[str, Any] | None:
        return render_delivery(delivery_admin.summary_delivery(obj))

    @extend_schema_field(CancellationSerializer(allow_null=True))
    def get_cancellation(self, obj: Order) -> dict[str, Any] | None:
        value = selectors.cancellation(obj)
        return None if value is None else dict(CancellationSerializer(value).data)


class OrderItemSerializer(serializers.Serializer[Any]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    billz_id = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
    image_thumbnail_url = serializers.CharField(allow_null=True, help_text="400 px WebP variant.")
    image_medium_url = serializers.CharField(allow_null=True, help_text="1000 px WebP variant.")
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
    dispatch = serializers.BooleanField(
        help_text="Yandex Delivery: `POST orders/{id}/dispatch/` would be accepted now (false for BTS)."
    )
    confirm_return = serializers.BooleanField(
        help_text="Yandex Delivery: the parcel is returning / returned and its return is not confirmed yet "
        "(`POST orders/{id}/confirm-return/`)."
    )


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
    delivery = serializers.SerializerMethodField()

    @extend_schema_field(SHIPMENT_SCHEMA)
    def get_shipment(self, obj: selectors.OrderDetail) -> dict[str, Any]:
        return render_shipment(obj.shipment)

    @extend_schema_field(OrderDeliverySerializer(allow_null=True))
    def get_delivery(self, obj: selectors.OrderDetail) -> dict[str, Any] | None:
        return render_delivery(obj.delivery)

    @extend_schema_field(CancellationSerializer(allow_null=True))
    def get_cancellation(self, obj: selectors.OrderDetail) -> dict[str, Any] | None:
        value = selectors.cancellation(obj.order)
        return None if value is None else dict(CancellationSerializer(value).data)

    def get_address(self, obj: selectors.OrderDetail) -> str | None:
        return obj.order.address_text or None


__all__ = [
    "AvailableActionsSerializer",
    "CancellationSerializer",
    "CashbackHoldSerializer",
    "OrderCashbackSerializer",
    "OrderDetailSerializer",
    "OrderEventSerializer",
    "OrderItemSerializer",
    "OrderPaymentSerializer",
    "OrderSummarySerializer",
    "PaymentSummarySerializer",
]
