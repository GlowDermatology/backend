"""The admin products: list (with its export), retrieve, create, update, and the stats."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.catalog import billz, selectors, services
from glowderma.catalog.api.admin.schemas.products import (
    PRODUCT_CREATE,
    PRODUCT_LIST,
    PRODUCT_PARTIAL_UPDATE,
    PRODUCT_RETRIEVE,
    PRODUCT_STATS_GET,
)
from glowderma.catalog.api.admin.serializers import (
    ProductListQuerySerializer,
    ProductPatchSerializer,
    ProductSerializer,
    ProductStatsSerializer,
    ProductWriteSerializer,
)
from glowderma.catalog.api.admin.views.common import query_params
from glowderma.catalog.models import Product
from glowderma.core import labels
from glowderma.core.exports import ExportMixin
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.integrations import selectors as integration_selectors


class ProductViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = ProductSerializer
    page_size_options = (8,)
    default_page_size = 8
    export_name = N_("mahsulotlar")
    export_filter_keys = frozenset(ProductListQuerySerializer().fields)
    export_resource = "products"
    export_headers = labels.PRODUCT_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Product]:
        query = ProductListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        filters = cast(selectors.ProductFilters, dict(query.validated_data))
        return selectors.filter_products(selectors.product_queryset(), filters)

    def get_export_rows(self, items: Iterable[Product]) -> Iterable[Sequence[Any]]:
        for product in items:
            yield (
                product.pk,
                product.billz_id,
                product.billz_name,
                product.name_uz,
                product.category.name_uz if product.category_id and product.category else "",
                product.price,
                int(getattr(product, "stock_quantity", 0)),
                labels.yes_no(product.promo),
                labels.text(labels.PRODUCT_STATUS, product.status),
                labels.yes_no(product.is_new_from_billz),
            )

    @PRODUCT_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @PRODUCT_CREATE
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        services.require_live_billz()  # before the serializer's Billz lookup (customer contract §6.14)
        serializer = ProductWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = services.create_product(serializer.to_input(), actor=request.user, request=request)
        return Response(ProductSerializer(product).data, status=201)

    @PRODUCT_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        product = selectors.get_product(pk)
        if product is None:
            raise NotFound
        return Response(ProductSerializer(product).data)

    @PRODUCT_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        product = Product.objects.filter(pk=pk).first()
        if product is None:
            raise NotFound
        data = request.data
        submitted = data.get("billz_id") if isinstance(data, dict) else None
        # Compare what the serializer would store (a trimmed string); null, booleans and other types fall
        # through to its 400 without a Billz lookup.
        if (
            isinstance(submitted, (str, int))
            and not isinstance(submitted, bool)
            and str(submitted).strip() != product.billz_id
        ):
            services.require_live_billz()  # before the serializer's Billz lookup (customer contract §6.14)
        serializer = ProductPatchSerializer(instance=product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_product(
            product.pk, serializer.to_input(), actor=request.user, request=request
        )
        return Response(ProductSerializer(updated).data)


class ProductStatsView(AdminAPIView):
    @PRODUCT_STATS_GET
    def get(self, request: Request) -> Response:
        stats: dict[str, Any] = selectors.product_counts()
        stats["billz"] = integration_selectors.billz_status(configured=billz.get_port().is_configured())
        return Response(ProductStatsSerializer(stats).data)


__all__ = [
    "ProductStatsView",
    "ProductViewSet",
]
