"""OpenAPI declarations for the admin banners: list (with its export), create, update, delete and order."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

from glowderma.catalog.api.admin.serializers import (
    BannerListQuerySerializer,
    BannerPatchSerializer,
    BannerResultsSerializer,
    BannerSerializer,
    IdOrderSerializer,
)
from glowderma.core import enums
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM

BannerFormSerializer = inline_serializer(
    name="BannerForm",
    fields={
        "image": serializers.FileField(required=False),
        "title.uz": serializers.CharField(max_length=80),
        "title.ru": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "title.en": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "link.kind": serializers.ChoiceField(choices=enums.BannerLinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False, allow_null=True),
        "link.category": serializers.CharField(required=False, allow_null=True, max_length=40),
        "link.url": serializers.CharField(required=False, allow_null=True, max_length=500),
        "is_active": serializers.BooleanField(required=False),
        "starts_at": serializers.DateTimeField(required=False, allow_null=True),
        "ends_at": serializers.DateTimeField(required=False, allow_null=True),
    },
)

BannerPatchFormSerializer = inline_serializer(
    name="BannerPatchForm",
    fields={
        "image": serializers.FileField(required=False),
        "title.uz": serializers.CharField(max_length=80, required=False),
        "title.ru": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "title.en": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "link.kind": serializers.ChoiceField(choices=enums.BannerLinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False, allow_null=True),
        "link.category": serializers.CharField(required=False, allow_null=True, max_length=40),
        "link.url": serializers.CharField(required=False, allow_null=True, max_length=500),
        "is_active": serializers.BooleanField(required=False),
        "starts_at": serializers.DateTimeField(required=False, allow_null=True),
        "ends_at": serializers.DateTimeField(required=False, allow_null=True),
    },
)

BANNER_LIST = extend_schema(
    operation_id="admin_banners_list",
    parameters=[BannerListQuerySerializer, EXPORT_PARAM],
    responses={200: BannerSerializer(many=True), **ADMIN_ERRORS},
)

BANNER_CREATE = extend_schema(
    operation_id="admin_banners_create",
    description="Multipart: `image` plus dotted keys `title.uz`, `link.kind`, … (BannerWriteSerializer).",
    request={"multipart/form-data": BannerFormSerializer},
    responses={201: BannerSerializer, **ADMIN_ERRORS},
)

BANNER_RETRIEVE = extend_schema(
    operation_id="admin_banners_retrieve", responses={200: BannerSerializer, **ADMIN_ERRORS}
)

BANNER_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_banners_partial_update",
    request={"multipart/form-data": BannerPatchFormSerializer, "application/json": BannerPatchSerializer},
    responses={200: BannerSerializer, **ADMIN_ERRORS},
)

BANNER_DESTROY = extend_schema(operation_id="admin_banners_destroy", responses={204: None, **ADMIN_ERRORS})

BANNER_REORDER = extend_schema(
    operation_id="admin_banners_reorder",
    request=IdOrderSerializer,
    responses={200: BannerResultsSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "BANNER_CREATE",
    "BANNER_DESTROY",
    "BANNER_LIST",
    "BANNER_PARTIAL_UPDATE",
    "BANNER_REORDER",
    "BANNER_RETRIEVE",
    "BannerFormSerializer",
    "BannerPatchFormSerializer",
]
