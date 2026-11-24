"""OpenAPI declarations for the admin products: list (with its export), retrieve, create, update, and the
stats."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.catalog.api.admin.serializers import (
    ProductListQuerySerializer,
    ProductPatchSerializer,
    ProductSerializer,
    ProductStatsSerializer,
    ProductWriteSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM, ErrorEnvelopeSerializer

# A create or a billz_id change asks Billz for the product: a live Billz that does not answer is 503.
BILLZ_LOOKUP_ERRORS = {**ADMIN_ERRORS, 503: ErrorEnvelopeSerializer}

PRODUCT_LIST = extend_schema(
    operation_id="admin_products_list",
    parameters=[ProductListQuerySerializer, EXPORT_PARAM],
    responses={200: ProductSerializer(many=True), **ADMIN_ERRORS},
)

PRODUCT_CREATE = extend_schema(
    operation_id="admin_products_create",
    request=ProductWriteSerializer,
    responses={201: ProductSerializer, **BILLZ_LOOKUP_ERRORS},
)

PRODUCT_RETRIEVE = extend_schema(
    operation_id="admin_products_retrieve", responses={200: ProductSerializer, **ADMIN_ERRORS}
)

PRODUCT_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_products_partial_update",
    request=ProductPatchSerializer,
    responses={200: ProductSerializer, **BILLZ_LOOKUP_ERRORS},
)

PRODUCT_STATS_GET = extend_schema(
    operation_id="admin_products_stats", responses={200: ProductStatsSerializer, **ADMIN_ERRORS}
)


__all__ = [
    "BILLZ_LOOKUP_ERRORS",
    "PRODUCT_CREATE",
    "PRODUCT_LIST",
    "PRODUCT_PARTIAL_UPDATE",
    "PRODUCT_RETRIEVE",
    "PRODUCT_STATS_GET",
]
