"""The public catalog (customer contract §2.3, **O**): banners, categories, products. Optional auth: a present
bearer must be valid; a signed-in caller gets ``in_cart_quantity`` and ``Cache-Control: private,
no-cache``."""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.accounts.models import Customer
from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import Product
from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.pagination import EnvelopePagination
from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, OPTIONAL_DEVICE_HEADERS, PUBLIC_CUSTOMER_ERRORS
from glowderma.customers import selectors
from glowderma.customers.api.customer import serializers as s

PARAMETERS = [*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


def customer_id(request: Request) -> int | None:
    user: object = request.user
    return user.pk if isinstance(user, Customer) else None


class BannersView(PublicAPIView):
    public_cache_seconds = 60

    @extend_schema(
        operation_id="customer_catalog_banners_list",
        parameters=PARAMETERS,
        responses={200: s.CustomerBannerListSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        banners = selectors.customer_banners(timeutils.now())
        product_ids = [banner.link_product_id for banner in banners if banner.link_product_id]
        visible = selectors.visible_product_ids(product_ids)
        lang = self.language
        return Response({"results": [selectors.banner_payload(banner, lang, visible) for banner in banners]})


class CategoriesView(PublicAPIView):
    public_cache_seconds = 60

    @extend_schema(
        operation_id="customer_catalog_categories_list",
        parameters=PARAMETERS,
        responses={200: s.CustomerCategoryListSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        lang = self.language
        return Response(
            {"results": [selectors.category_payload(row, lang) for row in selectors.customer_categories()]}
        )


class ProductListView(PublicAPIView):
    public_cache_seconds = 30
    # Declared so drf-spectacular wraps the 200 in the ``Page<ProductCard>`` envelope (C§2.3) and documents
    # ``page`` / ``page_size`` from the paginator; the view paginates explicitly below.
    pagination_class = EnvelopePagination
    page_size_options = (20, 50)
    default_page_size = 20

    @extend_schema(
        operation_id="customer_catalog_products_list",
        parameters=[*PARAMETERS, s.ProductListQuerySerializer],
        responses={200: s.ProductCardSerializer(many=True), **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        query = s.ProductListQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        filters: dict[str, Any] = dict(query.validated_data)
        lang = self.language
        qs = selectors.customer_products()
        if filters.get("category"):
            category = selectors.active_category(filters["category"])
            if category is None:
                raise ValidationError({"category": [field_error("invalid_choice")]})
            qs = qs.filter(category=category)
        if len(filters.get("search", "")) >= 2:
            qs = catalog_selectors.filter_products_search(qs, filters["search"])
        if "promo" in filters:
            qs = qs.filter(promo=filters["promo"])
        if "in_stock" in filters:
            qs = selectors.filter_in_stock(qs, filters["in_stock"])
        qs = selectors.order_customer_products(qs, filters["ordering"], lang)
        paginator = self.pagination_class()
        page: list[Product] = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(selectors.product_cards(page, lang, customer_id(request)))


class ProductDetailView(PublicAPIView):
    public_cache_seconds = 30

    @extend_schema(
        operation_id="customer_catalog_products_retrieve",
        parameters=[*PARAMETERS, OpenApiParameter("id", OpenApiTypes.INT, OpenApiParameter.PATH)],
        responses={200: s.ProductDetailSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request, pk: int) -> Response:
        product = selectors.customer_products().filter(pk=pk).first()
        if product is None:
            raise NotFound
        return Response(selectors.product_detail(product, self.language, customer_id(request)))


class ProductBySlugView(PublicAPIView):
    """B-W1: the website addresses a product by its slug (``catalog/products/{slug}/``); a digits-only path
    segment is always the id route above (a slug is never only digits)."""

    public_cache_seconds = 30

    @extend_schema(
        operation_id="customer_catalog_products_by_slug_retrieve",
        description="One visible product by its website slug (B-W1); the same shape as the id route.",
        parameters=[*PARAMETERS, OpenApiParameter("slug", OpenApiTypes.STR, OpenApiParameter.PATH)],
        responses={200: s.ProductDetailSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request, slug: str) -> Response:
        product = selectors.customer_products().filter(slug=slug).first()
        if product is None:
            raise NotFound
        return Response(selectors.product_detail(product, self.language, customer_id(request)))
