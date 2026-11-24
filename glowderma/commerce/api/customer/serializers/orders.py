"""The customer's order shapes (customer contract §2.8): the scope query, the summary and the detail."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums


class CustomerOrderScopeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    scope = serializers.ChoiceField(
        choices=enums.OrderScope.values, required=False, default=enums.OrderScope.ALL
    )


class CustomerSummaryPaymentSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=enums.PaymentStatus.values)
    status_label = serializers.CharField()
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    in_progress = serializers.BooleanField()


class CustomerOrderDeliverySerializer(serializers.Serializer[dict[str, Any]]):
    """The carrier view of a delivery order (plan §3.4); a BTS order has only ``provider``."""

    provider = serializers.ChoiceField(choices=enums.DeliveryProvider.values)
    method = serializers.ChoiceField(choices=enums.DeliveryMethod.values, allow_null=True)
    method_label = serializers.CharField(allow_null=True)
    stage = serializers.ChoiceField(choices=enums.ShipmentStage.values, allow_null=True)
    stage_label = serializers.CharField(allow_null=True)
    eta_from = serializers.DateTimeField(allow_null=True)
    eta_to = serializers.DateTimeField(allow_null=True)
    courier_name = serializers.CharField(allow_null=True)
    courier_vehicle = serializers.CharField(allow_null=True)
    tracking_url = serializers.CharField(allow_null=True)


class CustomerOrderSummarySerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=enums.OrderStatus.values)
    status_label = serializers.CharField()
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    total = serializers.IntegerField()
    item_count = serializers.IntegerField()
    preview_images = serializers.ListField(child=serializers.CharField())
    preview_thumbnail_images = serializers.ListField(
        child=serializers.CharField(), help_text="`preview_images` as their 400 px WebP variants, same order."
    )
    payment = CustomerSummaryPaymentSerializer()
    pay_by = serializers.DateTimeField(allow_null=True)
    tracking_number = serializers.CharField(allow_null=True)
    delivery = CustomerOrderDeliverySerializer(allow_null=True, help_text="null for pickup.")


class CustomerDetailItemSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
    image_thumbnail_url = serializers.CharField(allow_null=True, help_text="400 px WebP variant.")
    image_medium_url = serializers.CharField(allow_null=True, help_text="1000 px WebP variant.")
    quantity = serializers.IntegerField()
    unit_price = serializers.IntegerField()
    promo = serializers.BooleanField()
    line_total = serializers.IntegerField()


class CustomerDetailPaymentSerializer(CustomerSummaryPaymentSerializer):
    amount = serializers.IntegerField()
    confirmed_at = serializers.DateTimeField(allow_null=True)
    in_progress_label = serializers.CharField(allow_null=True)


class CustomerRefundSerializer(serializers.Serializer[dict[str, Any]]):
    state = serializers.ChoiceField(choices=enums.CustomerRefundState.values)
    state_label = serializers.CharField()
    amount = serializers.IntegerField()
    requested_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)


class CustomerShipmentSerializer(serializers.Serializer[dict[str, Any]]):
    carrier = serializers.CharField()
    tracking_number = serializers.CharField()
    tracking_url = serializers.CharField(allow_null=True)


class CustomerOrderCashbackSerializer(serializers.Serializer[dict[str, Any]]):
    used = serializers.IntegerField()
    earned = serializers.IntegerField()
    earned_state = serializers.ChoiceField(choices=enums.CashbackEarnedState.values)


class CustomerCancellationSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.ChoiceField(choices=enums.CancellationReason.values)
    label = serializers.CharField()  # type: ignore[assignment]


class CustomerProgressStepSerializer(serializers.Serializer[dict[str, Any]]):
    step = serializers.ChoiceField(choices=enums.ProgressStep.values)
    label = serializers.CharField()  # type: ignore[assignment]
    state = serializers.ChoiceField(choices=enums.ProgressState.values)
    at = serializers.DateTimeField(allow_null=True)


class CustomerTimelineEntrySerializer(serializers.Serializer[dict[str, Any]]):
    type = serializers.ChoiceField(choices=enums.TimelineType.values)
    at = serializers.DateTimeField()
    title = serializers.CharField()
    subtitle = serializers.CharField(allow_null=True)


class CustomerOrderActionsSerializer(serializers.Serializer[dict[str, Any]]):
    pay = serializers.BooleanField()
    cancel = serializers.BooleanField()
    reorder = serializers.BooleanField()


class CustomerOrderDetailSerializer(CustomerOrderSummarySerializer):
    fulfilment_label = serializers.CharField()
    items = CustomerDetailItemSerializer(many=True)
    items_subtotal = serializers.IntegerField()
    promo_subtotal = serializers.IntegerField()
    delivery_fee = serializers.IntegerField()
    cashback_used = serializers.IntegerField()
    address = serializers.CharField(allow_null=True)
    address_comment = serializers.CharField(allow_null=True)
    pickup_address = serializers.CharField(allow_null=True)
    customer_note = serializers.CharField(allow_null=True)
    payment = CustomerDetailPaymentSerializer()
    refund = CustomerRefundSerializer(allow_null=True)
    shipment = CustomerShipmentSerializer(allow_null=True)
    cashback = CustomerOrderCashbackSerializer()
    cancellation = CustomerCancellationSerializer(allow_null=True)
    progress = CustomerProgressStepSerializer(many=True)
    timeline = CustomerTimelineEntrySerializer(many=True)
    actions = CustomerOrderActionsSerializer()


__all__ = [
    "CustomerCancellationSerializer",
    "CustomerDetailItemSerializer",
    "CustomerDetailPaymentSerializer",
    "CustomerOrderActionsSerializer",
    "CustomerOrderCashbackSerializer",
    "CustomerOrderDeliverySerializer",
    "CustomerOrderDetailSerializer",
    "CustomerOrderScopeQuerySerializer",
    "CustomerOrderSummarySerializer",
    "CustomerProgressStepSerializer",
    "CustomerRefundSerializer",
    "CustomerShipmentSerializer",
    "CustomerSummaryPaymentSerializer",
    "CustomerTimelineEntrySerializer",
]
