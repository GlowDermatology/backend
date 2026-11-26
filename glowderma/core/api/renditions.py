"""The ``renditions`` objects of product images (``core.renditions``): a 4:5 ``portrait`` and a ``square``
copy, each ``null`` until staff (or a scene) set it, so a client falls back to the image itself."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums


class RenditionUrlsSerializer(serializers.Serializer[dict[str, Any]]):
    url = serializers.URLField(help_text="Full-size WebP of the shape (long side ≤ 2000 px).")
    medium_url = serializers.URLField(help_text="1000 px WebP variant.")
    thumbnail_url = serializers.URLField(help_text="400 px WebP variant.")


class ImageRenditionsSerializer(serializers.Serializer[dict[str, Any]]):
    portrait = RenditionUrlsSerializer(
        allow_null=True, help_text="4:5 crop (product cards, product page, cart); null = use the image."
    )
    square = RenditionUrlsSerializer(
        allow_null=True, help_text="1:1 crop (home rails, feed, app cards, thumbnails); null = use the image."
    )


class AdminRenditionSerializer(RenditionUrlsSerializer):
    crop = serializers.ListField(
        child=serializers.FloatField(),
        min_length=4,
        max_length=4,
        help_text="[x, y, w, h], 0-1 of the image.",
    )
    mode = serializers.ChoiceField(choices=enums.RenditionMode.values)


class AdminImageRenditionsSerializer(serializers.Serializer[dict[str, Any]]):
    portrait = AdminRenditionSerializer(allow_null=True)
    square = AdminRenditionSerializer(allow_null=True)
