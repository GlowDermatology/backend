"""Serializers of the commerce customer routes (customer contract §2.5–§2.8): request bodies with the
contract's field codes, and the response shapes (``Cart``, ``DeliveryQuote``, ``PayLink``, ``OrderSummary``,
``OrderDetail``) rendered from the dictionaries ``commerce.selectors`` builds."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums

QUANTITY_MAX = 99
NOTE_MAX = 300


# --- requests
# --------------------------------------------------------------------------------------------------


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
    # plan §3.4 (all optional; a request without them is answered exactly as before)
    with_options = serializers.BooleanField(
        required=False,
        allow_null=True,
        default=None,  # not ``False``: generated clients would read an emitted default as a required key
        help_text="List every delivery option (Yandex Standard / Express, BTS) with its fee, ETA and quote.",
    )
    delivery_provider = serializers.ChoiceField(
        choices=enums.DeliveryProvider.values,
        required=False,
        allow_null=True,
        default=None,
        help_text="Preferred carrier; the answer falls back to the default option when it is unavailable.",
    )
    delivery_method = serializers.ChoiceField(
        choices=enums.DeliveryMethod.values,
        required=False,
        allow_null=True,
        default=None,
        help_text="Preferred Yandex method (implies `delivery_provider=yandex`).",
    )


class CustomerCheckoutSerializer(serializers.Serializer[dict[str, Any]]):
    fulfilment = serializers.ChoiceField(choices=enums.FulfilmentMethod.values)
    address_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    payment_provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    cashback_to_use = serializers.IntegerField(required=False, default=0, min_value=0)
    expected_total = serializers.IntegerField(min_value=0)
    customer_note = serializers.CharField(required=False, default="", allow_blank=True, max_length=NOTE_MAX)
    delivery_quote_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        default=None,
        help_text="A Yandex `quote_id` from `delivery/calculate/`; without it delivery ships by BTS.",
    )


class CustomerPaySerializer(serializers.Serializer[dict[str, Any]]):
    provider = serializers.ChoiceField(
        choices=enums.PaymentProvider.values, required=False, allow_null=True, default=None
    )


class CustomerReorderSerializer(serializers.Serializer[dict[str, Any]]):
    cancel_unpaid = serializers.BooleanField(required=False, default=False)


class CustomerOrderScopeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    scope = serializers.ChoiceField(
        choices=enums.OrderScope.values, required=False, default=enums.OrderScope.ALL
    )


# --- responses
# ------------------------------------------------------------------------------------------------


class CustomerCartLineSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    volume = serializers.CharField()
    main_image_url = serializers.CharField(allow_null=True)
    main_image_thumbnail_url = serializers.CharField(allow_null=True, help_text="400 px WebP variant.")
    main_image_medium_url = serializers.CharField(allow_null=True, help_text="1000 px WebP variant.")
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


class CustomerDeliveryOptionSerializer(serializers.Serializer[dict[str, Any]]):
    provider = serializers.ChoiceField(choices=enums.DeliveryProvider.values)
    method = serializers.ChoiceField(choices=enums.DeliveryMethod.values, allow_null=True)
    label = serializers.CharField()  # type: ignore[assignment]
    available = serializers.BooleanField()
    unavailable_reason = serializers.ChoiceField(
        choices=enums.DeliveryUnavailableReason.values, allow_null=True
    )
    fee = serializers.IntegerField(allow_null=True, help_text="so'm; null when unavailable (never invented).")
    eta_from = serializers.DateTimeField(allow_null=True)
    eta_to = serializers.DateTimeField(allow_null=True)
    scheduled = serializers.BooleanField(help_text="The window starts at a later warehouse opening.")
    quote_id = serializers.UUIDField(allow_null=True, help_text="Pass to checkout as `delivery_quote_id`.")
    expires_at = serializers.DateTimeField(allow_null=True)
    expected_total = serializers.IntegerField(allow_null=True)


class CustomerDeliveryQuoteBaseSerializer(serializers.Serializer[dict[str, Any]]):
    """The released answer of ``delivery/calculate/`` (a request without the plan §3.4 keys)."""

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


class CustomerDeliveryQuoteSerializer(CustomerDeliveryQuoteBaseSerializer):
    """``DeliveryQuote``: the released keys, plus — only when the request sent ``with_options``,
    ``delivery_provider`` or ``delivery_method`` — the selected option and every option."""

    provider = serializers.ChoiceField(choices=enums.DeliveryProvider.values, allow_null=True, required=False)
    method = serializers.ChoiceField(choices=enums.DeliveryMethod.values, allow_null=True, required=False)
    quote_id = serializers.UUIDField(allow_null=True, required=False)
    quote_expires_at = serializers.DateTimeField(allow_null=True, required=False)
    options = CustomerDeliveryOptionSerializer(many=True, required=False)


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
    checkout_order = CustomerOrderDetailSerializer(allow_null=True, required=False)
