"""The bodies of the admin order actions: cancel, transition, dispatch and confirm a return."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.commerce.api.admin.serializers.delivery_issues import clean_note
from glowderma.commerce.api.admin.serializers.messages import (
    MESSAGE_REASON_MAX,
    MESSAGE_REASON_MIN,
    REASON_MAX_LENGTH,
    REASON_MIN_LENGTH,
)
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_


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


MESSAGE_CONTENTS_UNVERIFIED = N_("Jo‘natma tarkibini tekshirganingizni tasdiqlang.")


DISPATCH_WEIGHT_MAX_G = 20_000


DISPATCH_PRICE_MAX = 100_000_000


class DispatchSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST orders/{id}/dispatch/`` (Yandex Delivery plan §3.6): staff checked the parcel and weighed it."""

    contents_verified = serializers.BooleanField(help_text="Must be `true`: staff checked the contents.")
    weight_g = serializers.IntegerField(min_value=1, max_value=DISPATCH_WEIGHT_MAX_G, help_text="Grams.")
    approved_price = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
        max_value=DISPATCH_PRICE_MAX,
        help_text="So'm (VAT-inclusive): the courier price staff approved; only while a `dispatch_review` "
        "issue is open.",
    )
    method = serializers.ChoiceField(
        choices=enums.DeliveryMethod.values,
        required=False,
        allow_null=True,
        default=None,
        help_text="Switch the delivery method (the fee the customer paid never changes).",
    )

    def validate_contents_verified(self, value: bool) -> bool:
        if value is not True:
            raise serializers.ValidationError(field_error("invalid", MESSAGE_CONTENTS_UNVERIFIED))
        return value


class ConfirmReturnSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST orders/{id}/confirm-return/`` (Yandex Delivery plan §3.8): the parcel is physically back."""

    note = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        trim_whitespace=True,
        help_text="Optional (≤ 1000): the parcel's condition; becomes the `return_pending` resolution note.",
    )

    def validate_note(self, value: str) -> str:
        return clean_note(value, required=False)


__all__ = [
    "DISPATCH_PRICE_MAX",
    "DISPATCH_WEIGHT_MAX_G",
    "MESSAGE_CONTENTS_UNVERIFIED",
    "CancelOrderSerializer",
    "ConfirmReturnSerializer",
    "DispatchSerializer",
    "TransitionSerializer",
]
