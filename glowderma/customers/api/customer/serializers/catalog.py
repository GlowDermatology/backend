"""Public catalog shapes (customer contract §2.3): the product list query, product cards and details, banners
and categories."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, QueryBooleanField
from glowderma.customers import selectors

SEARCH_MAX_LENGTH = 64


CATEGORY_SLUG_MAX_LENGTH = 64


class ProductListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    category = serializers.CharField(required=False)
    in_stock = QueryBooleanField(required=False)
    promo = QueryBooleanField(required=False)
    ordering = serializers.ChoiceField(
        choices=enums.CustomerProductOrdering.values,
        required=False,
        default=enums.CustomerProductOrdering.DEFAULT,
    )

    def validate_search(self, value: str) -> str:
        return value.strip()[:SEARCH_MAX_LENGTH]

    def validate_category(self, value: str) -> str:
        # An over-long slug can match no category: ``invalid_choice`` like any unknown slug (C§2.3).
        if len(value) > CATEGORY_SLUG_MAX_LENGTH:
            raise serializers.ValidationError([field_error("invalid_choice")])
        return value


class ProductFeedQuerySerializer(serializers.Serializer[dict[str, Any]]):
    cursor = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        help_text="Opaque: the `next_cursor` of the previous page; absent or empty = the first page.",
    )
    category = serializers.CharField(required=False, help_text="A category slug.")

    def validate_cursor(self, value: str) -> selectors.FeedCursor | None:
        if not value:
            return None
        cursor = selectors.decode_cursor(value)
        if cursor is None:
            raise serializers.ValidationError([field_error("invalid")])
        return cursor

    def validate_category(self, value: str) -> str:
        if len(value) > CATEGORY_SLUG_MAX_LENGTH:
            raise serializers.ValidationError([field_error("invalid_choice")])
        return value


class ProductCategorySerializer(serializers.Serializer[dict[str, Any]]):
    slug = serializers.CharField()
    name = serializers.CharField()


class ProductCardSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    slug = serializers.CharField(help_text="The website URL segment (B-W1); `catalog/products/{slug}/`.")
    name = serializers.CharField()
    content_language = serializers.ChoiceField(
        choices=enums.ContentLanguage.values,
        help_text=(
            "Language of `name` (and, where set, the other texts): the requested language, or `uz` when the "
            "name fell back to Uzbek or to the Billz name."
        ),
    )
    sku = serializers.CharField(
        allow_null=True,
        help_text="The Billz product id / barcode (`billz_id`), the shop's article code; null if unknown.",
    )
    volume = serializers.CharField()
    price = serializers.IntegerField()
    promo = serializers.BooleanField()
    category = ProductCategorySerializer(allow_null=True)
    main_image_url = serializers.URLField(allow_null=True)
    main_image_thumbnail_url = serializers.URLField(allow_null=True, help_text="400 px WebP variant.")
    main_image_medium_url = serializers.URLField(allow_null=True, help_text="1000 px WebP variant.")
    available = serializers.IntegerField()
    stock_status = serializers.ChoiceField(choices=enums.StockStatus.values)
    in_cart_quantity = serializers.IntegerField()
    updated_at = AwareDateTimeField(help_text="Last change of the product page (sitemap `lastmod`).")


class ProductFeedSerializer(serializers.Serializer[dict[str, Any]]):
    results = ProductCardSerializer(many=True, help_text="Up to 24 cards, newest first.")
    next_cursor = serializers.CharField(allow_null=True, help_text="Pass as `cursor`; null on the last page.")


class CustomerProductImageSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    url = serializers.URLField()
    thumbnail_url = serializers.URLField(help_text="400 px WebP variant.")
    medium_url = serializers.URLField(help_text="1000 px WebP variant.")
    position = serializers.IntegerField()
    alt = serializers.CharField(
        help_text="Alt text in this language (else Uzbek); without one, the product name."
    )


class ProductDetailSerializer(ProductCardSerializer):
    short_description = serializers.CharField(
        help_text='One-paragraph summary in this language (else Uzbek); "" = none.'
    )
    description = serializers.CharField()
    ingredients = serializers.CharField()
    usage = serializers.CharField()
    images = CustomerProductImageSerializer(many=True)
    max_quantity = serializers.IntegerField()
    seo_title = serializers.CharField(help_text='Staff-written page title in this language; "" = none.')
    seo_description = serializers.CharField(help_text='Staff-written meta description; "" = none.')


class CustomerBannerLinkSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.BannerLinkKind.values)
    product_id = serializers.IntegerField(allow_null=True)
    category = serializers.CharField(allow_null=True)
    url = serializers.CharField(
        allow_null=True, help_text="Kind `url`: the https link the app opens; else null."
    )


class CustomerBannerSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    title = serializers.CharField()
    image_url = serializers.URLField(allow_null=True)
    image_thumbnail_url = serializers.URLField(allow_null=True, help_text="400 px WebP variant.")
    image_medium_url = serializers.URLField(allow_null=True, help_text="1000 px WebP variant.")
    image_width = serializers.IntegerField()
    image_height = serializers.IntegerField()
    link = CustomerBannerLinkSerializer()


class CustomerBannerListSerializer(serializers.Serializer[dict[str, Any]]):
    results = CustomerBannerSerializer(many=True)


class CustomerCategorySerializer(serializers.Serializer[dict[str, Any]]):
    slug = serializers.CharField()
    name = serializers.CharField()
    content_language = serializers.ChoiceField(
        choices=enums.ContentLanguage.values,
        help_text="Language of `name`: the requested language, or `uz` when it fell back to Uzbek.",
    )
    image_url = serializers.URLField(allow_null=True)
    image_thumbnail_url = serializers.URLField(allow_null=True, help_text="400 px WebP variant.")
    image_medium_url = serializers.URLField(allow_null=True, help_text="1000 px WebP variant.")
    product_count = serializers.IntegerField()
    updated_at = AwareDateTimeField()
    seo_title = serializers.CharField()
    seo_description = serializers.CharField()


class CustomerCategoryListSerializer(serializers.Serializer[dict[str, Any]]):
    results = CustomerCategorySerializer(many=True)


__all__ = [
    "CATEGORY_SLUG_MAX_LENGTH",
    "SEARCH_MAX_LENGTH",
    "CustomerBannerLinkSerializer",
    "CustomerBannerListSerializer",
    "CustomerBannerSerializer",
    "CustomerCategoryListSerializer",
    "CustomerCategorySerializer",
    "CustomerProductImageSerializer",
    "ProductCardSerializer",
    "ProductCategorySerializer",
    "ProductDetailSerializer",
    "ProductFeedQuerySerializer",
    "ProductFeedSerializer",
    "ProductListQuerySerializer",
]
