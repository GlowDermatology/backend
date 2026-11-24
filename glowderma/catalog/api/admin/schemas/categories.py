"""OpenAPI declarations for the admin categories: list (with its export), create, update, delete and order."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

from glowderma.catalog.api.admin.serializers import (
    CategoryListQuerySerializer,
    CategoryPatchSerializer,
    CategoryResultsSerializer,
    CategorySerializer,
    CategoryWriteSerializer,
    SlugOrderSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM

CategoryFormSerializer = inline_serializer(
    name="CategoryForm",
    fields={
        "slug": serializers.CharField(max_length=40),
        "name.uz": serializers.CharField(min_length=2, max_length=60),
        "name.ru": serializers.CharField(max_length=60, required=False, allow_blank=True),
        "name.en": serializers.CharField(max_length=60, required=False, allow_blank=True),
        "is_active": serializers.BooleanField(required=False),
        "image": serializers.FileField(required=False),
        **{
            f"seo.{language}.{leaf}": serializers.CharField(
                max_length=limit, required=False, allow_blank=True
            )
            for language in ("uz", "ru", "en")
            for leaf, limit in (("title", 120), ("description", 300))
        },
    },
)

CategoryPatchFormSerializer = inline_serializer(
    name="CategoryPatchForm",
    fields={
        "name.uz": serializers.CharField(min_length=2, max_length=60, required=False),
        "name.ru": serializers.CharField(max_length=60, required=False, allow_blank=True),
        "name.en": serializers.CharField(max_length=60, required=False, allow_blank=True),
        "is_active": serializers.BooleanField(required=False),
        "image": serializers.FileField(required=False),
        "remove_image": serializers.BooleanField(required=False),
        **{
            f"seo.{language}.{leaf}": serializers.CharField(
                max_length=limit, required=False, allow_blank=True
            )
            for language in ("uz", "ru", "en")
            for leaf, limit in (("title", 120), ("description", 300))
        },
    },
)

CATEGORY_LIST = extend_schema(
    operation_id="admin_categories_list",
    parameters=[CategoryListQuerySerializer, EXPORT_PARAM],
    responses={200: CategorySerializer(many=True), **ADMIN_ERRORS},
)

CATEGORY_CREATE = extend_schema(
    operation_id="admin_categories_create",
    description="JSON, or multipart with dotted `name.uz`/`name.ru`/`name.en` and an optional `image` "
    "(JPEG, PNG or WebP, ≤ 5 MiB; re-encoded, EXIF stripped, ≤ 2400 px).",
    request={"application/json": CategoryWriteSerializer, "multipart/form-data": CategoryFormSerializer},
    responses={201: CategorySerializer, **ADMIN_ERRORS},
)

CATEGORY_RETRIEVE = extend_schema(
    operation_id="admin_categories_retrieve", responses={200: CategorySerializer, **ADMIN_ERRORS}
)

CATEGORY_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_categories_partial_update",
    description="JSON or multipart (dotted names). A multipart `image` replaces the stored image (the "
    "old file is removed); `remove_image: true` drops it; both at once → `remove_image` `invalid`.",
    request={
        "application/json": CategoryPatchSerializer,
        "multipart/form-data": CategoryPatchFormSerializer,
    },
    responses={200: CategorySerializer, **ADMIN_ERRORS},
)

CATEGORY_DESTROY = extend_schema(
    operation_id="admin_categories_destroy", responses={204: None, **ADMIN_ERRORS}
)

CATEGORY_REORDER = extend_schema(
    operation_id="admin_categories_reorder",
    request=SlugOrderSerializer,
    responses={200: CategoryResultsSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CATEGORY_CREATE",
    "CATEGORY_DESTROY",
    "CATEGORY_LIST",
    "CATEGORY_PARTIAL_UPDATE",
    "CATEGORY_REORDER",
    "CATEGORY_RETRIEVE",
    "CategoryFormSerializer",
    "CategoryPatchFormSerializer",
]
