"""``delivery/calculate/`` shapes (customer contract §2.6): the body, the options and the quote."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums


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


__all__ = [
    "CustomerDeliveryCalculateSerializer",
    "CustomerDeliveryOptionSerializer",
    "CustomerDeliveryQuoteBaseSerializer",
    "CustomerDeliveryQuoteSerializer",
]
