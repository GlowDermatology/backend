"""Read-only Django admin for the commerce models (design 03 §8). Writes go through ``commerce.services``."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, OrderItem, StockReservation
from glowderma.core.admin import ReadOnlyAdmin, register_read_only

if TYPE_CHECKING:
    _TabularInline = admin.TabularInline[models.Model, models.Model]
else:
    _TabularInline = admin.TabularInline  # the runtime class is not generic


class ReadOnlyTabularInline(_TabularInline):
    """An inline that only lists rows: no add/change/delete, every field read-only."""

    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: models.Model | None = None) -> list[str]:
        # ``fields`` is declared flat (no fieldset-style nesting) on every inline below.
        return [name for name in self.fields or () if isinstance(name, str)]


class OrderItemInline(ReadOnlyTabularInline):
    model = OrderItem
    fields = ("position", "name", "billz_id", "quantity", "unit_price", "promo", "line_total")
    ordering = ("position",)


class OrderEventInline(ReadOnlyTabularInline):
    model = OrderEvent
    fields = ("at", "type", "source", "status_after", "actor")
    ordering = ("-at", "-id")
    raw_id_fields = ("actor",)


class OrderAdmin(ReadOnlyAdmin):
    list_display = ("id", "customer", "status", "fulfilment", "total", "payment_provider", "created_at")
    list_filter = ("status", "fulfilment", "payment_provider", "channel")
    search_fields = ("=id", "customer__phone", "customer__name")
    ordering = ("-created_at", "-id")
    raw_id_fields = ("customer", "cancelled_by")
    inlines = (OrderItemInline, OrderEventInline)
    date_hierarchy = "created_at"


admin.site.register(Order, OrderAdmin)
register_read_only(
    OrderItem,
    list_display=("order", "position", "name", "billz_id", "quantity", "unit_price", "promo"),
    search_fields=("=order__id", "billz_id", "name"),
    ordering=("-order_id", "position"),
    raw_id_fields=("order", "product"),
)
register_read_only(
    OrderEvent,
    list_display=("id", "order", "at", "type", "source", "status_after", "actor"),
    list_filter=("type", "source"),
    search_fields=("=order__id",),
    ordering=("-at", "-id"),
    raw_id_fields=("order", "actor"),
    date_hierarchy="at",
)
register_read_only(
    StockReservation,
    list_display=("id", "order", "product", "quantity", "state", "expires_at"),
    list_filter=("state", "release_reason"),
    search_fields=("=order__id", "product__billz_id"),
    ordering=("-id",),
    raw_id_fields=("order", "product"),
)
register_read_only(
    Cart,
    list_display=("id", "customer", "updated_at", "last_checkout_order"),
    search_fields=("customer__phone",),
    ordering=("-updated_at",),
    raw_id_fields=("customer", "last_checkout_order"),
)
register_read_only(
    CartItem,
    list_display=("id", "cart", "product", "quantity", "added_at", "updated_at"),
    search_fields=("cart__customer__phone", "product__billz_id"),
    ordering=("cart_id", "added_at", "id"),
    raw_id_fields=("cart", "product"),
)
