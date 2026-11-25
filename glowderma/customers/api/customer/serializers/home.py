"""The storefront landing page (``GET home/``): the shop's numbers, the spotlight, the product rails and the
cashback tiers. Response shapes for the OpenAPI schema only (the view returns the selector's dictionary)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.fields import PercentField
from glowderma.customers.api.customer.serializers.catalog import ProductCardSerializer


class HomeStatsSerializer(serializers.Serializer[dict[str, Any]]):
    products = serializers.IntegerField(help_text="Visible products.")
    products_in_stock = serializers.IntegerField(
        allow_null=True,
        help_text="Visible products a customer can buy now; null while online selling is off.",
    )
    categories = serializers.IntegerField(help_text="Active categories (as `catalog/categories/` lists).")
    specialists = serializers.IntegerField(help_text="Listed specialists (as `specialists/` lists).")
    clinics = serializers.IntegerField(help_text="Active clinics (as `clinics/` lists).")


class HomeSpotlightSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.ChoiceField(choices=enums.HomeSpotlightReason.values)
    product = ProductCardSerializer()


class HomeRailSerializer(serializers.Serializer[dict[str, Any]]):
    key = serializers.ChoiceField(choices=enums.HomeRailKey.values)
    items = ProductCardSerializer(many=True)


class HomeCashbackTierSerializer(serializers.Serializer[dict[str, Any]]):
    from_amount = serializers.IntegerField(help_text="Lifetime purchases from (so'm, inclusive).")
    to_amount = serializers.IntegerField(
        allow_null=True, help_text="Up to (exclusive); null = no upper bound."
    )
    percent = PercentField()


class HomeCashbackSerializer(serializers.Serializer[dict[str, Any]]):
    min_percent = PercentField()
    max_percent = PercentField()
    tiers = HomeCashbackTierSerializer(many=True)


class HomeSerializer(serializers.Serializer[dict[str, Any]]):
    content_language = serializers.ChoiceField(
        choices=enums.ContentLanguage.values, help_text="The response language (Accept-Language)."
    )
    online_sales_enabled = serializers.BooleanField()
    stats = HomeStatsSerializer()
    spotlight = HomeSpotlightSerializer(
        many=True,
        help_text=(
            "Up to 6 products with an image, each once, taken one per reason in turn (promo, bestseller, "
            "low_stock, new), rotated by the Tashkent hour."
        ),
    )
    rails = HomeRailSerializer(
        many=True,
        help_text="`new`, `promo`, `bestsellers` (at least 4 items) in that order; empty rails are left out.",
    )
    cashback = HomeCashbackSerializer(allow_null=True, help_text="The active cashback policy; null = none.")
    feed_cursor = serializers.CharField(
        allow_null=True,
        help_text=(
            "The `catalog/feed/` cursor right after the `new` rail's last product (the landing's unfiltered "
            "feed starts there, so it does not repeat the rail); null = no products."
        ),
    )


__all__ = [
    "HomeCashbackSerializer",
    "HomeCashbackTierSerializer",
    "HomeRailSerializer",
    "HomeSerializer",
    "HomeSpotlightSerializer",
    "HomeStatsSerializer",
]
