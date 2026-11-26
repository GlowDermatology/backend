"""OpenAPI declarations for A product's images: upload, alt texts, removal and order."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

from glowderma.catalog.api.admin.serializers import (
    ImageOrderSerializer,
    ImagePatchSerializer,
    ImageRenditionsPutSerializer,
    ImagesResponseSerializer,
    ProductImageSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS

PRODUCT_IMAGES_POST = extend_schema(
    operation_id="admin_product_images_create",
    description="One multipart part `file` (JPEG, PNG or WebP, ≤ 5 MiB, ≤ 8 images per product).",
    request={
        "multipart/form-data": inline_serializer(
            name="ProductImageUpload", fields={"file": serializers.FileField()}
        )
    },
    responses={201: ProductImageSerializer, **ADMIN_ERRORS},
)

PRODUCT_IMAGE_DETAIL_PATCH = extend_schema(
    operation_id="admin_product_images_partial_update",
    request=ImagePatchSerializer,
    responses={200: ProductImageSerializer, **ADMIN_ERRORS},
)

PRODUCT_IMAGE_DETAIL_DELETE = extend_schema(
    operation_id="admin_product_images_destroy", responses={204: None, **ADMIN_ERRORS}
)

PRODUCT_IMAGES_ORDER_PUT = extend_schema(
    operation_id="admin_product_images_reorder",
    request=ImageOrderSerializer,
    responses={200: ImagesResponseSerializer, **ADMIN_ERRORS},
)

PRODUCT_IMAGE_RENDITIONS_PUT = extend_schema(
    operation_id="admin_product_images_renditions",
    description=(
        "Set the screen shapes of an image: a 4:5 `portrait` and a `square` crop, stored as WebP copies "
        "(the image itself is never changed). `invalid_crop` / `crop_too_small` per shape."
    ),
    request=ImageRenditionsPutSerializer,
    responses={200: ProductImageSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "PRODUCT_IMAGE_RENDITIONS_PUT",
    "PRODUCT_IMAGES_ORDER_PUT",
    "PRODUCT_IMAGES_POST",
    "PRODUCT_IMAGE_DETAIL_DELETE",
    "PRODUCT_IMAGE_DETAIL_PATCH",
]
