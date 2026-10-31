"""Read-only Django admin for the catalog (design 01 §11). Writes go through the admin API only."""

from typing import Any

from django.contrib import admin
from django.db.models import Model, QuerySet
from django.http import HttpRequest

from glowderma.catalog import selectors
from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.core import timeutils
from glowderma.core.admin import (
    SUCCESS,
    WARNING,
    dropdown,
    money_column,
    register_read_only,
    status_column,
)
from glowderma.core.labels import PRODUCT_STATUS


class StockStateFilter(admin.SimpleListFilter):
    """«Qoldiq»: the stock problems the «Savdo va katalog» dashboard counts (catalog.selectors.stock_health),
    so its tiles link to exactly the rows behind their numbers (an id list stops at the first hundred)."""

    title = "qoldiq"
    parameter_name = "stock"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            ("out", "Tugagan (0 yoki Billz’da yozuvi yo‘q)"),
            ("stale", "Eskirgan (24 soatdan beri yangilanmagan)"),
            ("in", "Bor"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Model]) -> QuerySet[Model]:
        if self.value() == "out":
            return queryset.filter(selectors.OUT_OF_STOCK)
        if self.value() == "stale":
            return queryset.filter(selectors.stale_stock(timeutils.now() - selectors.STALE_STOCK_AFTER))
        if self.value() == "in":
            return queryset.exclude(selectors.OUT_OF_STOCK)
        return queryset


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
    list_filter=(dropdown("status"), StockStateFilter, "promo", "is_new_from_billz", "category"),
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
