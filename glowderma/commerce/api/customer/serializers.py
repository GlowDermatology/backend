"""Serializers of the commerce customer routes (customer contract §2.5–§2.8): request bodies with the contract's
field codes, and the response shapes (``Cart``, ``DeliveryQuote``, ``PayLink``, ``OrderSummary``,
``OrderDetail``) rendered from the dictionaries ``commerce.selectors`` builds."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums

QUANTITY_MAX = 99
NOTE_MAX = 300


# --- requests --------------------------------------------------------------------------------------------------


class CustomerCartAddSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=QUANTITY_MAX, default=1)


class CustomerCartQuantitySerializer(serializers.Serializer[dict[str, Any]]):
    quantity = serializers.IntegerField(min_value=1, max_value=QUANTITY_MAX)


class CustomerDeliveryCalculateSerializer(serializers.Serializer[dict[str, Any]]):
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    address_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    region_code = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None, max_length=16
    )
    city_code = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None, max_length=16
    )
    cashback_to_use = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=0)


class CustomerCheckoutSerializer(serializers.Serializer[dict[str, Any]]):
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    address_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    payment_provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    cashback_to_use = serializers.IntegerField(required=False, default=0, min_value=0)
    expected_total = serializers.IntegerField(min_value=0)
    customer_note = serializers.CharField(required=False, default="", allow_blank=True, max_length=NOTE_MAX)


class CustomerPaySerializer(serializers.Serializer[dict[str, Any]]):
    provider = serializers.ChoiceField(
        choices=enums.PaymentProvider.values, required=False, allow_null=True, default=None
    )


class CustomerOrderScopeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    scope = serializers.ChoiceField(
        choices=enums.OrderScope.values, required=False, default=enums.OrderScope.ALL
    )


# --- responses ------------------------------------------------------------------------------------------------


class CustomerCartLineSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    volume = serializers.CharField()
    main_image_url = serializers.CharField(allow_null=True)
    unit_price = serializers.IntegerField()
    promo = serializers.BooleanField()
    quantity = serializers.IntegerField()
    line_total = serializers.IntegerField()
    available = serializers.IntegerField()
    max_quantity = serializers.IntegerField()
    max_reached = serializers.BooleanField()
    status = serializers.ChoiceField(choices=enums.CartLineStatus.values)
    price_changed = serializers.BooleanField()
    previous_price = serializers.IntegerField(allow_null=True)


class CustomerCartCashbackSerializer(serializers.Serializer[dict[str, Any]]):
    available = serializers.IntegerField()
    max_usable_pickup = serializers.IntegerField()


class CustomerCartChangeSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    requested_quantity = serializers.IntegerField()
    quantity = serializers.IntegerField()
    max_reached = serializers.BooleanField()


class CustomerCartSerializer(serializers.Serializer[dict[str, Any]]):
    items = CustomerCartLineSerializer(many=True)
    item_count = serializers.IntegerField()
    quantity_total = serializers.IntegerField()
    items_subtotal = serializers.IntegerField()
    promo_subtotal = serializers.IntegerField()
    has_problems = serializers.BooleanField()
    cashback = CustomerCartCashbackSerializer()
    updated_at = serializers.DateTimeField(allow_null=True)
    last_change = CustomerCartChangeSerializer(allow_null=True)


class CustomerDeliveryQuoteSerializer(serializers.Serializer[dict[str, Any]]):
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    delivery_fee = serializers.IntegerField()
    region_code = serializers.CharField(allow_null=True)
    city_code = serializers.CharField(allow_null=True)
    weight_g = serializers.IntegerField(allow_null=True)
    pickup_address = serializers.CharField(allow_null=True)
    quoted_at = serializers.DateTimeField()
    items_subtotal = serializers.IntegerField()
    cashback_max = serializers.IntegerField()
    cashback_to_use = serializers.IntegerField()
    expected_total = serializers.IntegerField()


class CustomerPayLinkSerializer(serializers.Serializer[dict[str, Any]]):
    order_id = serializers.IntegerField()
    payment_id = serializers.IntegerField()
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    amount = serializers.IntegerField()
    pay_by = serializers.DateTimeField()
    account_ref = serializers.CharField()
    web_url = serializers.CharField()
    app_url = serializers.CharField(allow_null=True)
    return_url = serializers.CharField()


class CustomerSummaryPaymentSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=enums.PaymentStatus.values)
    status_label = serializers.CharField()
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    in_progress = serializers.BooleanField()


class CustomerOrderSummarySerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    status = serializers.ChoiceField(choices=enums.OrderStatus.values)
    status_label = serializers.CharField()
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    total = serializers.IntegerField()
    item_count = serializers.IntegerField()
    preview_images = serializers.ListField(child=serializers.CharField())
    payment = CustomerSummaryPaymentSerializer()
    pay_by = serializers.DateTimeField(allow_null=True)
    tracking_number = serializers.CharField(allow_null=True)


class CustomerDetailItemSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    image_url = serializers.CharField(allow_null=True)
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
