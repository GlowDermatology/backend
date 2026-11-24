"""OpenAPI declarations for the public catalog (customer contract §2.3, **O**): banners, categories, products.
Optional auth: a present bearer must be valid; a signed-in caller gets ``in_cart_quantity`` and
``Cache-Control: private, no-cache``."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.core.api.schema import (
    PUBLIC_CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import PUBLIC_PARAMETERS

BANNERS_GET = extend_schema(
    operation_id="customer_catalog_banners_list",
    parameters=PUBLIC_PARAMETERS,
    responses={200: s.CustomerBannerListSerializer, **PUBLIC_CUSTOMER_ERRORS},
)

CATEGORIES_GET = extend_schema(
    operation_id="customer_catalog_categories_list",
    parameters=PUBLIC_PARAMETERS,
    responses={200: s.CustomerCategoryListSerializer, **PUBLIC_CUSTOMER_ERRORS},
)

PRODUCT_LIST_GET = extend_schema(
    operation_id="customer_catalog_products_list",
    parameters=[*PUBLIC_PARAMETERS, s.ProductListQuerySerializer],
    responses={200: s.ProductCardSerializer(many=True), **PUBLIC_CUSTOMER_ERRORS},
)

PRODUCT_DETAIL_GET = extend_schema(
    operation_id="customer_catalog_products_retrieve",
    parameters=[*PUBLIC_PARAMETERS, OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
    responses={200: s.ProductDetailSerializer, **PUBLIC_CUSTOMER_ERRORS},
)

PRODUCT_BY_SLUG_GET = extend_schema(
    operation_id="customer_catalog_products_by_slug_retrieve",
    description="One visible product by its website slug (B-W1); the same shape as the id route.",
    parameters=[*PUBLIC_PARAMETERS, OpenApiParameter("slug", OpenApiTypes.STR, OpenApiParameter.PATH)],
    responses={200: s.ProductDetailSerializer, **PUBLIC_CUSTOMER_ERRORS},
)


__all__ = [
    "BANNERS_GET",
    "CATEGORIES_GET",
    "PRODUCT_BY_SLUG_GET",
    "PRODUCT_DETAIL_GET",
    "PRODUCT_LIST_GET",
]
