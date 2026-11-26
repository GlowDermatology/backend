"""The server cart shapes (customer contract §2.5): the item bodies, the lines, the cashback and the cart."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.commerce.api.customer.serializers.orders import CustomerOrderDetailSerializer
from glowderma.core import enums
from glowderma.core.api.renditions import ImageRenditionsSerializer

QUANTITY_MAX = 99


class CustomerCartAddSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=QUANTITY_MAX, default=1)


class CustomerCartQuantitySerializer(serializers.Serializer[dict[str, Any]]):
    quantity = serializers.IntegerField(min_value=1, max_value=QUANTITY_MAX)


class CustomerCartLineSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    name = serializers.CharField()
    volume = serializers.CharField()
    main_image_url = serializers.CharField(allow_null=True)
    main_image_thumbnail_url = serializers.CharField(allow_null=True, help_text="400 px WebP variant.")
    main_image_medium_url = serializers.CharField(allow_null=True, help_text="1000 px WebP variant.")
    main_image_renditions = ImageRenditionsSerializer(
        help_text="Screen shapes of the main image (null each = none)."
    )
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
    checkout_order = CustomerOrderDetailSerializer(allow_null=True, required=False)


__all__ = [
    "QUANTITY_MAX",
    "CustomerCartAddSerializer",
    "CustomerCartCashbackSerializer",
    "CustomerCartChangeSerializer",
    "CustomerCartLineSerializer",
    "CustomerCartQuantitySerializer",
    "CustomerCartSerializer",
]
