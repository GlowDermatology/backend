"""The admin category: the translated name, the row, the list query, the writes and the ordered results."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.catalog import seo
from glowderma.catalog.api.admin.serializers.fields import UZ_NAME_MIN_LENGTH, StrictBooleanField
from glowderma.catalog.api.admin.serializers.seo_texts import (
    SeoStatusSerializer,
    SeoTextsSerializer,
    seo_texts,
)
from glowderma.catalog.models import Category
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    QueryBooleanField,
    SlugField,
    i18n_dict,
    i18n_text,
)
from glowderma.core.media import absolute_media_url, optional_variant_url


class CategoryNameSerializer(serializers.Serializer[dict[str, str]]):
    """Contract §2.4 ``name``: ``uz`` 2..60 required; ``ru`` / ``en`` ≤ 60 optional (missing on create → "").

    The core ``i18n_text`` shape requires every language; the catalog write shapes only require Uzbek.
    """

    uz = serializers.CharField(max_length=60, min_length=UZ_NAME_MIN_LENGTH)
    ru = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")


class CategorySerializer(serializers.Serializer[Category]):
    """Contract §2.4 ``Category``; instances come from ``selectors.category_queryset()``."""

    slug = serializers.CharField()
    name = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    image_thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    image_medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
    position = serializers.IntegerField()
    is_active = serializers.BooleanField()
    product_count = serializers.IntegerField()
    seo = serializers.SerializerMethodField()
    seo_status = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(60))
    def get_name(self, obj: Category) -> dict[str, str]:
        return i18n_dict(obj, "name")

    @extend_schema_field(SeoTextsSerializer())
    def get_seo(self, obj: Category) -> dict[str, dict[str, str]]:
        return seo_texts(obj)

    @extend_schema_field(SeoStatusSerializer())
    def get_seo_status(self, obj: Category) -> dict[str, Any]:
        active = getattr(obj, "active_product_count", None)
        if active is None:
            active = obj.products.filter(status=enums.ProductStatus.ACTIVE).count()
        return seo.status_payload(seo.category_issues(obj, active_products=int(active)))

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_image_url(self, obj: Category) -> str | None:
        """Gap ADM-5.1: the category image (absolute, ``PUBLIC_MEDIA_BASE_URL``) | ``null``."""
        return absolute_media_url(obj.image.name) if obj.image.name else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_image_thumbnail_url(self, obj: Category) -> str | None:
        return optional_variant_url(obj.image.name, "thumbnail")

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_image_medium_url(self, obj: Category) -> str | None:
        return optional_variant_url(obj.image.name, "medium")


class CategoryListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    is_active = QueryBooleanField(required=False)


class CategoryWriteSerializer(serializers.Serializer[Any]):
    """``POST categories/`` and ``PATCH categories/{slug}/`` (``partial``; ``slug`` is then ``read_only``).

    JSON, or multipart with the dotted names ``name.uz``, ``name.ru``, ``name.en`` next to ``slug``,
    ``is_active`` and the optional file part ``image`` (gap ADM-5.1; the view reads the file and un-dots the
    names with ``views.form_to_nested``).
    """

    slug = SlugField()
    name = CategoryNameSerializer()
    is_active = serializers.BooleanField(required=False, default=True)
    seo = SeoTextsSerializer(required=False)

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if self.instance is not None and isinstance(data, dict) and "slug" in data:
            raise serializers.ValidationError({"slug": [field_error("read_only")]})
        return cast(dict[str, Any], super().to_internal_value(data))

    def validate_slug(self, value: str) -> str:
        if Category.objects.filter(slug=value).exists():
            raise serializers.ValidationError([field_error("unique")])
        return value


class CategoryPatchSerializer(CategoryWriteSerializer):
    """``PATCH categories/{slug}/``: every field optional; ``remove_image: true`` drops the stored image
    (gap ADM-5.1; refused together with a new ``image`` part: ``remove_image`` ``invalid``)."""

    remove_image = StrictBooleanField(required=False, default=False)


class CategoryResultsSerializer(serializers.Serializer[dict[str, Any]]):
    results = CategorySerializer(many=True)


__all__ = [
    "CategoryListQuerySerializer",
    "CategoryNameSerializer",
    "CategoryPatchSerializer",
    "CategoryResultsSerializer",
    "CategorySerializer",
    "CategoryWriteSerializer",
]
