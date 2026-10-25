"""Admin serializers of `catalog` (contract §2.3 Products, §2.4 Categories, §2.5 Banners).

Write serializers validate shapes and resolve references (category slugs, Billz ids, banner link targets)
and hand a ``services.ProductInput`` / ``services.BannerInput`` to the services. Read serializers render the
contract shapes from the selectors' annotated querysets (``selectors.product_queryset`` and friends).
"""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.catalog import billz, selectors, services
from glowderma.catalog.models import Banner, Category, Product, ProductImage
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, QueryBooleanField, SlugField, i18n_dict, i18n_text
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url
from glowderma.integrations.billz.ports import BillzPort, BillzProduct

BILLZ_ID_PATTERN = r"^\d{6,14}$"
UZ_NAME_MIN_LENGTH = 2

# --- shared -------------------------------------------------------------------------------------------------


class CategoryNameSerializer(serializers.Serializer[dict[str, str]]):
    """Contract §2.4 ``name``: ``uz`` 2..60 required; ``ru`` / ``en`` ≤ 60 optional (missing on create → "").

    The core ``i18n_text`` shape requires every language; the catalog write shapes only require Uzbek.
    """

    uz = serializers.CharField(max_length=60, min_length=UZ_NAME_MIN_LENGTH)
    ru = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")


class BannerTitleSerializer(serializers.Serializer[dict[str, str]]):
    """Contract §2.5 ``title``: ``uz`` 1..80 required; ``ru`` / ``en`` ≤ 80 optional (missing → "")."""

    uz = serializers.CharField(max_length=80)
    ru = serializers.CharField(max_length=80, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=80, allow_blank=True, required=False, default="")


class ProductImageSerializer(serializers.Serializer[ProductImage]):
    """Contract §2.3 ``ProductImage = {id, url, position}``."""

    id = serializers.IntegerField()
    url = serializers.SerializerMethodField()
    position = serializers.IntegerField()

    def get_url(self, obj: ProductImage) -> str:
        return absolute_media_url(obj.file.name or "")


class ProductTextSerializer(serializers.Serializer[dict[str, str]]):
    """One language of ``texts``: ``{name, description, ingredients, usage}``."""

    name = serializers.CharField(max_length=120, allow_blank=True, required=False)
    description = serializers.CharField(max_length=2000, allow_blank=True, required=False)
    ingredients = serializers.CharField(max_length=1000, allow_blank=True, required=False)
    usage = serializers.CharField(max_length=1000, allow_blank=True, required=False)


class ProductUzTextSerializer(ProductTextSerializer):
    """The Uzbek texts: ``name`` is mandatory on create and, whenever sent, trimmed 2..120 characters."""

    name = serializers.CharField(max_length=120, allow_blank=True)

    def validate_name(self, value: str) -> str:
        if len(value) < UZ_NAME_MIN_LENGTH:
            raise serializers.ValidationError(
                [field_error("min_length", "O‘zbekcha nomni kiriting (kamida 2 belgi).")]
            )
        return value


class ProductTextsSerializer(serializers.Serializer[dict[str, Any]]):
    """Language-first product texts ``{uz: {...}, ru: {...}, en: {...}}`` (AGENTS.md: not ``i18n_text``)."""

    uz = ProductUzTextSerializer()
    ru = ProductTextSerializer(required=False)
    en = ProductTextSerializer(required=False)


class LastChangeSerializer(serializers.Serializer[Product]):
    at = serializers.DateTimeField(source="last_change_at")
    actor = ActorSerializer(source="last_change_actor", allow_null=True)
    kind = serializers.ChoiceField(choices=enums.ProductChangeKind.values, source="last_change_kind")
    old_price = serializers.IntegerField(source="last_change_old_price", allow_null=True)
    new_price = serializers.IntegerField(source="last_change_new_price", allow_null=True)


# --- products: read -----------------------------------------------------------------------------------------


def product_texts(product: Product) -> dict[str, dict[str, str]]:
    return {
        language: {field: getattr(product, f"{field}_{language}") or "" for field in services.TEXT_FIELDS}
        for language in services.LANGUAGES
    }


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
    images = serializers.SerializerMethodField()
    last_change = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

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


# --- products: query and write ------------------------------------------------------------------------------


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
        error_messages={"invalid": "Billz ID yoki shtrix-kod 6–14 ta raqamdan iborat bo‘lsin."},
    )
    texts = ProductTextsSerializer()
    price = serializers.IntegerField(
        min_value=services.MIN_PRICE,
        max_value=100_000_000,
        error_messages={"min_value": "Narx kamida 1 000 so‘m bo‘lsin.", "max_value": "Narx juda katta."},
    )
    category = serializers.CharField(allow_null=True, required=False, max_length=40)
    promo = serializers.BooleanField(required=False, default=False)
    status = serializers.ChoiceField(
        choices=enums.ProductStatus.values, required=False, default=enums.ProductStatus.HIDDEN
    )

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
        found = self._port().get_product(value)
        if found is None:
            raise serializers.ValidationError(
                [field_error("billz_not_found", "Billz’da bunday mahsulot topilmadi.")]
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
        )


class ImageOrderSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT products/{id}/images/order/``."""

    # ``[]`` is a valid body: it is the exact current set of a product without images (200, no-op); for a
    # non-empty set the service answers ``mismatch`` — never DRF's untranslated ``empty``.
    image_ids = serializers.ListField(child=serializers.IntegerField())


class ImagesResponseSerializer(serializers.Serializer[dict[str, Any]]):
    images = ProductImageSerializer(many=True)


# --- categories ---------------------------------------------------------------------------------------------


class CategorySerializer(serializers.Serializer[Category]):
    """Contract §2.4 ``Category``; instances come from ``selectors.category_queryset()``."""

    slug = serializers.CharField()
    name = serializers.SerializerMethodField()
    position = serializers.IntegerField()
    is_active = serializers.BooleanField()
    product_count = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(60))
    def get_name(self, obj: Category) -> dict[str, str]:
        return i18n_dict(obj, "name")


class CategoryListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    is_active = QueryBooleanField(required=False)


class CategoryWriteSerializer(serializers.Serializer[Any]):
    """``POST categories/`` and ``PATCH categories/{slug}/`` (``partial``; ``slug`` is then ``read_only``)."""

    slug = SlugField()
    name = CategoryNameSerializer()
    is_active = serializers.BooleanField(required=False, default=True)

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if self.instance is not None and isinstance(data, dict) and "slug" in data:
            raise serializers.ValidationError({"slug": [field_error("read_only")]})
        return cast(dict[str, Any], super().to_internal_value(data))

    def validate_slug(self, value: str) -> str:
        if Category.objects.filter(slug=value).exists():
            raise serializers.ValidationError([field_error("unique")])
        return value


class SlugOrderSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT categories/order/``."""

    slugs = serializers.ListField(child=serializers.CharField(max_length=40))  # ``[]`` → mismatch, or no-op


class CategoryResultsSerializer(serializers.Serializer[dict[str, Any]]):
    results = CategorySerializer(many=True)


# --- banners ------------------------------------------------------------------------------------------------


class BannerLinkSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.BannerLinkKind.values, required=False)
    product_id = serializers.IntegerField(required=False, allow_null=True)
    category = serializers.CharField(required=False, allow_null=True, max_length=40)


class BannerSerializer(serializers.Serializer[Banner]):
    """Contract §2.5 ``Banner``; instances come from ``selectors.banner_queryset()``."""

    id = serializers.IntegerField()
    title = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    link = serializers.SerializerMethodField()
    is_active = serializers.BooleanField()
    starts_at = serializers.DateTimeField(allow_null=True)
    ends_at = serializers.DateTimeField(allow_null=True)
    position = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(80))
    def get_title(self, obj: Banner) -> dict[str, str]:
        return i18n_dict(obj, "title")

    @extend_schema_field(serializers.URLField())
    def get_image_url(self, obj: Banner) -> str:
        return absolute_media_url(obj.image.name or "")

    @extend_schema_field(BannerLinkSerializer())
    def get_link(self, obj: Banner) -> dict[str, Any]:
        kind = obj.link_kind
        return {
            "kind": kind,
            "product_id": obj.link_product_id if kind == enums.BannerLinkKind.PRODUCT else None,
            "category": (
                obj.link_category.slug
                if kind == enums.BannerLinkKind.CATEGORY and obj.link_category_id and obj.link_category
                else None
            ),
        }


class BannerListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    is_active = QueryBooleanField(required=False)


class BannerWriteSerializer(serializers.Serializer[Any]):
    """Fields of ``POST banners/`` (multipart) and ``PATCH banners/{id}/`` (multipart or JSON; ``partial``).

    Accepted form encoding (multipart): nested keys arrive as dotted form names — ``title.uz``, ``title.ru``,
    ``title.en``, ``link.kind``, ``link.product_id``, ``link.category`` — next to the flat ``is_active``,
    ``starts_at``, ``ends_at`` and the file part ``image``. The view un-dots them into this nested shape
    (``views.form_to_nested``); an empty string for ``link.product_id``, ``link.category``, ``starts_at`` or
    ``ends_at`` means ``null``. Booleans are ``true``/``false``/``1``/``0``. Error paths use the same dotted
    names, so multipart and JSON callers map errors identically.

    The ``image`` part is read from the request by the view (it is not a field here); the link targets and
    the date order are validated against the merged state (``instance`` on PATCH).
    """

    title = BannerTitleSerializer()
    link = BannerLinkSerializer(required=False)
    is_active = serializers.BooleanField(required=False, default=True)
    starts_at = AwareDateTimeField(required=False, allow_null=True)
    ends_at = AwareDateTimeField(required=False, allow_null=True)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.link_product: Product | None = None
        self.link_category: Category | None = None
        self.link_kind: str | None = None

    @property
    def banner(self) -> Banner | None:
        return cast(Banner | None, self.instance)

    def _resolve_link(self, link: dict[str, Any]) -> None:
        current = self.banner
        kind = link.get("kind", current.link_kind if current is not None else enums.BannerLinkKind.NONE)
        self.link_kind = kind
        if kind == enums.BannerLinkKind.PRODUCT:
            product_id = link.get("product_id", current.link_product_id if current is not None else None)
            if product_id is None:
                raise serializers.ValidationError({"link": {"product_id": [field_error("required")]}})
            product = Product.objects.filter(pk=product_id, status=enums.ProductStatus.ACTIVE).first()
            if product is None:
                raise serializers.ValidationError({"link": {"product_id": [field_error("does_not_exist")]}})
            self.link_product = product
        elif kind == enums.BannerLinkKind.CATEGORY:
            if "category" in link:
                slug = link["category"]
            elif current is not None and current.link_category_id and current.link_category:
                slug = current.link_category.slug
            else:
                slug = None
            if slug is None:
                raise serializers.ValidationError({"link": {"category": [field_error("required")]}})
            category = Category.objects.filter(slug=slug).first()
            if category is None:
                raise serializers.ValidationError({"link": {"category": [field_error("does_not_exist")]}})
            self.link_category = category

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        current = self.banner
        if "link" in attrs or current is None:
            self._resolve_link(attrs.get("link", {}))
        starts_at = attrs.get("starts_at", current.starts_at if current is not None else None)
        ends_at = attrs.get("ends_at", current.ends_at if current is not None else None)
        if starts_at is not None and ends_at is not None and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": [field_error("invalid")]})
        return attrs

    def to_input(self) -> services.BannerInput:
        data = self.validated_data
        return services.BannerInput(
            title=data.get("title"),
            link_kind=self.link_kind,
            link_product=self.link_product,
            link_category=self.link_category,
            is_active=data.get("is_active"),
            starts_at=data.get("starts_at"),
            starts_at_set="starts_at" in data,
            ends_at=data.get("ends_at"),
            ends_at_set="ends_at" in data,
        )


class IdOrderSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT banners/order/``."""

    ids = serializers.ListField(child=serializers.IntegerField())  # ``[]`` → mismatch unless no banners


class BannerResultsSerializer(serializers.Serializer[dict[str, Any]]):
    results = BannerSerializer(many=True)
