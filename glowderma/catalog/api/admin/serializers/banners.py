"""The admin banner: the translated title, the link, the row, the list query, the writes and the ordered
results."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.catalog import services
from glowderma.catalog.models import Banner, Category, Product
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    AwareDateTimeField,
    HttpsUrlField,
    QueryBooleanField,
    i18n_dict,
    i18n_text,
)
from glowderma.core.media import absolute_media_url, optional_variant_url


class BannerTitleSerializer(serializers.Serializer[dict[str, str]]):
    """Contract §2.5 ``title``: ``uz`` 1..80 required; ``ru`` / ``en`` ≤ 80 optional (missing → "")."""

    uz = serializers.CharField(max_length=80)
    ru = serializers.CharField(max_length=80, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=80, allow_blank=True, required=False, default="")


class BannerTitlePatchSerializer(BannerTitleSerializer):
    """``title`` of ``PATCH banners/{id}/``: any subset of the three leaves (merged into the stored title)."""

    uz = serializers.CharField(max_length=80, required=False)
    ru = serializers.CharField(max_length=80, allow_blank=True, required=False)
    en = serializers.CharField(max_length=80, allow_blank=True, required=False)


class BannerLinkSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.BannerLinkKind.values, required=False)
    product_id = serializers.IntegerField(required=False, allow_null=True)
    category = serializers.CharField(required=False, allow_null=True, max_length=40)
    # gap X-BANNER-URL (decision GC-3): kind "url" only; an absolute https link ≤ 500
    url = HttpsUrlField(required=False, allow_null=True)


class BannerSerializer(serializers.Serializer[Banner]):
    """Contract §2.5 ``Banner``; instances come from ``selectors.banner_queryset()``."""

    id = serializers.IntegerField()
    title = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    image_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    image_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
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

    @extend_schema_field(serializers.URLField())
    def get_image_thumbnail_url(self, obj: Banner) -> str:
        return optional_variant_url(obj.image.name, "thumbnail") or ""

    @extend_schema_field(serializers.URLField())
    def get_image_medium_url(self, obj: Banner) -> str:
        return optional_variant_url(obj.image.name, "medium") or ""

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
            "url": (obj.link_url or None) if kind == enums.BannerLinkKind.URL else None,
        }


class BannerListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    is_active = QueryBooleanField(required=False)


class BannerWriteSerializer(serializers.Serializer[Any]):
    """Fields of ``POST banners/`` (multipart) and ``PATCH banners/{id}/`` (multipart or JSON; ``partial``).

    Accepted form encoding (multipart): nested keys arrive as dotted form names — ``title.uz``, ``title.ru``,
    ``title.en``, ``link.kind``, ``link.product_id``, ``link.category``, ``link.url`` — next to the flat
    ``is_active``, ``starts_at``, ``ends_at`` and the file part ``image``. The view un-dots them into this
    nested shape (``views.form_to_nested``); an empty string for ``link.product_id``, ``link.category``,
    ``link.url``, ``starts_at`` or ``ends_at`` means ``null``. Booleans are ``true``/``false``/``1``/``0``.
    Error paths use the same dotted names, so multipart and JSON callers map errors identically.

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
        self.link_url = ""
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
            unchanged = current is not None and product_id == current.link_product_id
            candidates = Product.objects.filter(pk=product_id)
            if not unchanged:
                candidates = candidates.filter(status=enums.ProductStatus.ACTIVE)
            product = candidates.first()
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
        elif kind == enums.BannerLinkKind.URL:
            if "url" in link:
                url = link["url"]
            else:  # PATCH without link.url keeps the stored one (only while the kind stays url)
                url = current.link_url if current is not None and current.link_kind == kind else None
            if not url:
                raise serializers.ValidationError({"link": {"url": [field_error("required")]}})
            self.link_url = url

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
            link_url=self.link_url,
            is_active=data.get("is_active"),
            starts_at=data.get("starts_at"),
            starts_at_set="starts_at" in data,
            ends_at=data.get("ends_at"),
            ends_at_set="ends_at" in data,
        )


class BannerPatchSerializer(BannerWriteSerializer):
    """``PATCH banners/{id}/`` (JSON or multipart): ``title`` merged leaf by leaf, every field optional.

    An unchanged ``link.product_id`` is not re-checked against the active products, so a banner whose product
    was hidden later can still be edited or switched off (contract §2.5).
    """

    title = BannerTitlePatchSerializer(required=False)


class BannerResultsSerializer(serializers.Serializer[dict[str, Any]]):
    results = BannerSerializer(many=True)


__all__ = [
    "BannerLinkSerializer",
    "BannerListQuerySerializer",
    "BannerPatchSerializer",
    "BannerResultsSerializer",
    "BannerSerializer",
    "BannerTitlePatchSerializer",
    "BannerTitleSerializer",
    "BannerWriteSerializer",
]
