"""Read-only Django admin for the catalog (design 01 §11). Writes go through the admin API only.

The product list's two stock filters are the «Savdo va katalog» dashboard's tiles as real filters (the tile
links with their parameters), so a drill-down lists every row behind the number, never a capped id list.
"""

from typing import Any

from django.contrib import admin
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils.dateparse import parse_datetime

from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.catalog.selectors import OUT_OF_STOCK
from glowderma.core.admin import (
    SUCCESS,
    WARNING,
    dropdown,
    money_column,
    register_read_only,
    status_column,
)
from glowderma.core.labels import PRODUCT_STATUS, stamp


class OutOfStockFilter(admin.SimpleListFilter):
    """«Tugagan»: no stock row or a quantity of 0 (``catalog.selectors.OUT_OF_STOCK``)."""

    title = "qoldiq"
    parameter_name = "out_of_stock"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("1", "Tugagan (0 yoki Billz’da yo‘q)")]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Product]) -> QuerySet[Product]:
        return queryset.filter(OUT_OF_STOCK) if self.value() == "1" else queryset


class StaleStockFilter(admin.SimpleListFilter):
    """«Qoldiq eskirgan»: a stock row not synced since the given instant (or never) — the dashboard's
    ``?stock_synced_before=<ISO>``; shown in the sidebar only while applied."""

    title = "qoldiq yangilanmagan"
    parameter_name = "stock_synced_before"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        value = self.value()
        parsed = parse_datetime(value) if value else None
        return [(str(value), f"{stamp(parsed)} dan beri")] if parsed is not None else []

    def queryset(self, request: HttpRequest, queryset: QuerySet[Product]) -> QuerySet[Product]:
        value = self.value()
        if not value:
            return queryset
        before = parse_datetime(value)
        if before is None:
            return queryset.none()
        stale = Q(stock__synced_at__lt=before) | Q(stock__synced_at__isnull=True)
        return queryset.filter(Q(stock__isnull=False) & stale)


register_read_only(
    Category,
    title_format="Kategoriya: {obj.name_uz}",
    list_display=("slug", "name_uz", "position", "is_active"),
    list_filter=("is_active",),
    search_fields=("slug", "name_uz"),
    ordering=("position", "id"),
)
register_read_only(
    Product,
    title_format="Mahsulot: {obj}",
    list_display=(
        "id",
        "billz_id",
        "name_uz",
        "billz_name",
        "category",
        money_column("price", "Narx"),
        status_column("status", "Holat", {"active": SUCCESS, "hidden": WARNING}, PRODUCT_STATUS),
        "promo",
        "is_new_from_billz",
    ),
    list_filter=(
        dropdown("status"),
        "promo",
        "is_new_from_billz",
        OutOfStockFilter,
        StaleStockFilter,
        "category",
    ),
    list_select_related=("category",),
    search_fields=("billz_id", "name_uz", "billz_name"),
    ordering=("-id",),
    autocomplete_fields=("category", "last_change_actor"),
)
register_read_only(
    ProductStock,
    list_display=("product", "quantity", "synced_at"),
    search_fields=("product__billz_id",),
    ordering=("product_id",),
    autocomplete_fields=("product",),
)
register_read_only(
    ProductImage,
    list_display=("product", "position", "content_type", "width", "height", "uploaded_by"),
    search_fields=("product__billz_id",),
    ordering=("product_id", "position"),
    autocomplete_fields=("product", "uploaded_by"),
)
register_read_only(
    Banner,
    title_format="Banner: {obj.title_uz}",
    list_display=("id", "title_uz", "link_kind", "is_active", "starts_at", "ends_at", "position"),
    list_filter=("link_kind", "is_active"),
    search_fields=("title_uz",),
    ordering=("position", "id"),
    autocomplete_fields=("link_product", "link_category"),
)
