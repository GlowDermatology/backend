"""The admin product: the row with its Billz status and last change, the stats, the list query and the
writes."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.catalog import billz, selectors, seo, services
from glowderma.catalog.api.admin.serializers.fields import BILLZ_ID_PATTERN
from glowderma.catalog.api.admin.serializers.product_images import ProductImageSerializer
from glowderma.catalog.api.admin.serializers.product_texts import (
    ProductTextsPatchSerializer,
    ProductTextsSerializer,
    product_texts,
)
from glowderma.catalog.api.admin.serializers.seo_texts import (
    SeoStatusSerializer,
    SeoTextsSerializer,
    seo_texts,
)
from glowderma.catalog.models import Category, Product, ProductImage
from glowderma.core import enums, slugs
from glowderma.core.api.exceptions import ProviderUnavailable, field_error
from glowderma.core.api.fields import (
    QueryBooleanField,
)
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url, optional_variant_url
from glowderma.core.uzsource import N_
from glowderma.integrations.billz.ports import BillzError, BillzPort, BillzProduct


def validate_product_slug(value: str, *, current: Product | None) -> str:
    """An edited product slug: ``^[a-z0-9]+(-[a-z0-9]+)*$``, not only digits, ≤140, unique."""
    value = value.strip()
    if not slugs.is_valid_slug(value, max_length=140):
        raise serializers.ValidationError(
            [
                field_error(
                    "invalid", N_("Faqat lotin kichik harflari, raqamlar va chiziqcha (faqat raqam emas).")
                )
            ]
        )
    others = Product.objects.filter(slug=value)
    if current is not None:
        others = others.exclude(pk=current.pk)
    if others.exists():
        raise serializers.ValidationError([field_error("unique", services.PRODUCT_SLUG_TAKEN)])
    return value


class LastChangeSerializer(serializers.Serializer[Product]):
    at = serializers.DateTimeField(source="last_change_at")
    actor = ActorSerializer(source="last_change_actor", allow_null=True)
    kind = serializers.ChoiceField(choices=enums.ProductChangeKind.values, source="last_change_kind")
    old_price = serializers.IntegerField(source="last_change_old_price", allow_null=True)
    new_price = serializers.IntegerField(source="last_change_new_price", allow_null=True)


class ProductSerializer(serializers.Serializer[Product]):
    """Contract §2.3 ``Product``; instances come from ``selectors.product_queryset()``."""

    id = serializers.IntegerField()
    billz_id = serializers.CharField()
    billz_name = serializers.CharField()
    volume = serializers.CharField()
    category = serializers.SerializerMethodField()
    price = serializers.IntegerField()
    billz_price = serializers.IntegerField(allow_null=True)
    stock = serializers.SerializerMethodField()
    promo = serializers.BooleanField()
    status = serializers.ChoiceField(choices=enums.ProductStatus.values)
    is_new_from_billz = serializers.BooleanField()
    texts = serializers.SerializerMethodField()
    main_image_url = serializers.SerializerMethodField()
    main_image_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    main_image_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
    images = serializers.SerializerMethodField()
    last_change = serializers.SerializerMethodField()
    slug = serializers.CharField(help_text="The website URL segment (B-W1): unique, editable.")
    seo = serializers.SerializerMethodField()
    seo_status = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(SeoTextsSerializer())
    def get_seo(self, obj: Product) -> dict[str, dict[str, str]]:
        return seo_texts(obj)

    @extend_schema_field(SeoStatusSerializer())
    def get_seo_status(self, obj: Product) -> dict[str, Any]:
        images = self._images(obj)
        return seo.status_payload(
            seo.product_issues(
                obj,
                image_count=len(images),
                images_without_alt=sum(1 for image in images if not image.alt_uz),
            )
        )

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_category(self, obj: Product) -> str | None:
        return obj.category.slug if obj.category_id is not None and obj.category else None

    @extend_schema_field(serializers.IntegerField())
    def get_stock(self, obj: Product) -> int:
        quantity = getattr(obj, "stock_quantity", None)
        if quantity is None:
            quantity = selectors.billz_view(obj).stock
        return int(quantity)

    @extend_schema_field(ProductTextsSerializer())
    def get_texts(self, obj: Product) -> dict[str, dict[str, str]]:
        return product_texts(obj)

    def _images(self, obj: Product) -> list[ProductImage]:
        return list(obj.images.all())

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_main_image_url(self, obj: Product) -> str | None:
        images = self._images(obj)
        return absolute_media_url(images[0].file.name or "") if images else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_main_image_thumbnail_url(self, obj: Product) -> str | None:
        images = self._images(obj)
        return optional_variant_url(images[0].file.name, "thumbnail") if images else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_main_image_medium_url(self, obj: Product) -> str | None:
        images = self._images(obj)
        return optional_variant_url(images[0].file.name, "medium") if images else None

    @extend_schema_field(ProductImageSerializer(many=True))
    def get_images(self, obj: Product) -> list[dict[str, Any]]:
        return [cast(dict[str, Any], ProductImageSerializer(image).data) for image in self._images(obj)]

    @extend_schema_field(LastChangeSerializer(allow_null=True))
    def get_last_change(self, obj: Product) -> dict[str, Any] | None:
        if obj.last_change_at is None:
            return None
        return cast(dict[str, Any], LastChangeSerializer(obj).data)


class BillzStatusSerializer(serializers.Serializer[dict[str, Any]]):
    connected = serializers.BooleanField()
    last_sync_at = serializers.DateTimeField(allow_null=True)


class ProductStatsSerializer(serializers.Serializer[dict[str, Any]]):
    """Contract §2.3 ``GET products/stats/``."""

    total = serializers.IntegerField()
    active = serializers.IntegerField()
    hidden = serializers.IntegerField()
    new_from_billz = serializers.IntegerField()
    out_of_stock = serializers.IntegerField()
    billz = BillzStatusSerializer()


class ProductListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """Query params of ``GET products/`` (contract §2.3); ``search`` is trimmed to 64 characters."""

    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    category = serializers.CharField(
        required=False, help_text="A category slug, or `none` for products without a category."
    )
    status = serializers.ChoiceField(choices=enums.ProductStatus.values, required=False)
    promo = QueryBooleanField(required=False)
    out_of_stock = QueryBooleanField(required=False)
    is_new = QueryBooleanField(required=False)
    ordering = serializers.ChoiceField(
        choices=enums.ProductOrdering.values, required=False, default=enums.ProductOrdering.DEFAULT
    )

    def validate_category(self, value: str) -> Category | None:
        if value == "none":
            return None
        category = Category.objects.filter(slug=value).first()
        if category is None:
            raise serializers.ValidationError([field_error("invalid_choice")])
        return category


class ProductWriteSerializer(serializers.Serializer[Any]):
    """Body of ``POST products/`` (every rule of the §2.3 table) and ``PATCH products/{id}/`` (``partial``).

    ``context["billz"]`` may carry a ``BillzPort`` (tests); otherwise the configured adapter is used.
    """

    billz_id = serializers.RegexField(
        BILLZ_ID_PATTERN,
        error_messages={"invalid": N_("Billz ID yoki shtrix-kod 6–14 ta raqamdan iborat bo‘lsin.")},
    )
    texts = ProductTextsSerializer()
    price = serializers.IntegerField(
        min_value=services.MIN_PRICE,
        max_value=100_000_000,
        error_messages={
            "min_value": N_("Narx kamida 1 000 so‘m bo‘lsin."),
            "max_value": N_("Narx juda katta."),
        },
    )
    category = serializers.CharField(allow_null=True, required=False, max_length=40)
    promo = serializers.BooleanField(required=False, default=False)
    status = serializers.ChoiceField(
        choices=enums.ProductStatus.values, required=False, default=enums.ProductStatus.HIDDEN
    )
    slug = serializers.CharField(
        max_length=140, required=False, help_text="Optional on create (derived from the Uzbek name)."
    )
    seo = SeoTextsSerializer(required=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.billz_product: BillzProduct | None = None

    @property
    def product(self) -> Product | None:
        return cast(Product | None, self.instance)

    def _port(self) -> BillzPort:
        port = self.context.get("billz")
        return cast(BillzPort, port) if port is not None else billz.get_port()

    def validate_billz_id(self, value: str) -> str:
        current = self.product
        if current is not None and value == current.billz_id:
            return value  # unchanged: no refresh, no Billz round trip
        others = Product.objects.filter(billz_id=value)
        if current is not None:
            others = others.exclude(pk=current.pk)
        if others.exists():
            raise serializers.ValidationError([field_error("unique", services.BILLZ_ID_TAKEN)])
        try:
            found = self._port().get_product(value)
        except BillzError as exc:  # live Billz unreachable or refusing: 503, nothing written
            raise ProviderUnavailable() from exc
        if found is None:
            raise serializers.ValidationError(
                [field_error("billz_not_found", N_("Billz’da bunday mahsulot topilmadi."))]
            )
        self.billz_product = found
        return value

    def validate_category(self, value: str | None) -> Category | None:
        if value is None:
            return None
        category = Category.objects.filter(slug=value).first()
        if category is None:
            raise serializers.ValidationError([field_error("does_not_exist")])
        return category

    def validate_slug(self, value: str) -> str:
        return validate_product_slug(value, current=self.product)

    def to_input(self) -> services.ProductInput:
        data = self.validated_data
        return services.ProductInput(
            billz_id=data.get("billz_id"),
            billz_product=self.billz_product,
            texts=data.get("texts"),
            price=data.get("price"),
            category=data.get("category"),
            category_set="category" in data,
            promo=data.get("promo"),
            status=data.get("status"),
            slug=data.get("slug"),
            seo=data.get("seo"),
        )


class ProductPatchSerializer(ProductWriteSerializer):
    """``PATCH products/{id}/``: every field optional and ``texts`` merged leaf by leaf (contract §2.3).

    Validation is the create serializer's (``partial=True`` skips absent fields); the separate class gives the
    OpenAPI request its true shape, where no ``texts`` leaf is required.
    """

    texts = ProductTextsPatchSerializer(required=False)


__all__ = [
    "BillzStatusSerializer",
    "LastChangeSerializer",
    "ProductListQuerySerializer",
    "ProductPatchSerializer",
    "ProductSerializer",
    "ProductStatsSerializer",
    "ProductWriteSerializer",
    "validate_product_slug",
]
