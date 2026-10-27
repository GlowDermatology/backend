"""Read-only Django admin for the commerce models (design 03 §8). Writes go through ``commerce.services``."""

from django.contrib import admin

from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, OrderItem, StockReservation
from glowderma.core.admin import ReadOnlyAdmin, ReadOnlyTabularInline, register_read_only


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
