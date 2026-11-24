"""A product's images: the row with its translated alt texts, the order and the PATCH."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.catalog import services
from glowderma.catalog.models import ProductImage
from glowderma.core.media import absolute_media_url, optional_variant_url


class ImageAltSerializer(serializers.Serializer[dict[str, str]]):
    """``alt`` of a product image, ``{uz, ru, en}`` ≤160 each; ``""`` = the website uses the product name.
    On write every leaf is optional (merged leaf by leaf)."""

    uz = serializers.CharField(max_length=services.ALT_MAX_LENGTH, allow_blank=True, required=False)
    ru = serializers.CharField(max_length=services.ALT_MAX_LENGTH, allow_blank=True, required=False)
    en = serializers.CharField(max_length=services.ALT_MAX_LENGTH, allow_blank=True, required=False)


def image_alt(image: ProductImage) -> dict[str, str]:
    return {language: getattr(image, f"alt_{language}") or "" for language in services.LANGUAGES}


class ProductImageSerializer(serializers.Serializer[ProductImage]):
    """Contract §2.3 ``ProductImage = {id, url, thumbnail_url, medium_url, position, alt}``."""

    id = serializers.IntegerField()
    url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField(help_text="400 px WebP variant.")
    medium_url = serializers.SerializerMethodField(help_text="1000 px WebP variant.")
    position = serializers.IntegerField()
    alt = serializers.SerializerMethodField(help_text='Alt text per language; "" = the product name is used.')

    @extend_schema_field(ImageAltSerializer())
    def get_alt(self, obj: ProductImage) -> dict[str, str]:
        return image_alt(obj)

    def get_url(self, obj: ProductImage) -> str:
        return absolute_media_url(obj.file.name or "")

    def get_thumbnail_url(self, obj: ProductImage) -> str:
        return optional_variant_url(obj.file.name, "thumbnail") or ""

    def get_medium_url(self, obj: ProductImage) -> str:
        return optional_variant_url(obj.file.name, "medium") or ""


class ImageOrderSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT products/{id}/images/order/``."""

    # ``[]`` is a valid body: it is the exact current set of a product without images (200, no-op); for a
    # non-empty set the service answers ``mismatch`` — never DRF's untranslated ``empty``.
    image_ids = serializers.ListField(child=serializers.IntegerField())


class ImagePatchSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PATCH products/{id}/images/{image_id}/``: ``{alt: {uz?, ru?, en?}}``."""

    alt = ImageAltSerializer()


class ImagesResponseSerializer(serializers.Serializer[dict[str, Any]]):
    images = ProductImageSerializer(many=True)


__all__ = [
    "ImageAltSerializer",
    "ImageOrderSerializer",
    "ImagePatchSerializer",
    "ImagesResponseSerializer",
    "ProductImageSerializer",
    "image_alt",
]
