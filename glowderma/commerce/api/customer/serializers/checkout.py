"""Checkout, pay and reorder shapes (customer contract §2.7)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums

NOTE_MAX = 300


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


__all__ = [
    "NOTE_MAX",
    "CustomerCheckoutSerializer",
    "CustomerPayLinkSerializer",
    "CustomerPaySerializer",
    "CustomerReorderSerializer",
]
