"""Read-only Django admin for the commerce models (design 03 §8). Writes go through ``commerce.services``.

The order page groups its columns in tabs and shows the order's items, events, payments, shipment and cashback
holds as inline tabs (payments, delivery and loyalty sit below ``commerce`` in the layering).
"""

from django.contrib import admin

from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, OrderItem, StockReservation
from glowderma.core.admin import (
    DANGER,
    INFO,
    PRIMARY,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    customer_column,
    date_range,
    dropdown,
    money_column,
    register_read_only,
    status_column,
)
from glowderma.core.labels import ORDER_STATUS
from glowderma.delivery.models import Shipment
from glowderma.loyalty.models import CashbackHold
from glowderma.payments.models import Payment

ORDER_STATUS_VARIANTS = {
    "awaiting_payment": WARNING,
    "processing": INFO,
    "handed_to_courier": PRIMARY,
    "delivered": SUCCESS,
    "cancelled": DANGER,
}
RESERVATION_STATE_VARIANTS = {"active": INFO, "converted": SUCCESS}


class OrderItemInline(ReadOnlyTabularInline):
    model = OrderItem
    fields = ("position", "name", "billz_id", "quantity", "unit_price", "promo", "line_total")
    ordering = ("position",)
    tab = True
    verbose_name_plural = "Mahsulotlar"


class OrderEventInline(ReadOnlyTabularInline):
    model = OrderEvent
    fields = ("at", "type", "source", "status_after", "actor")
    ordering = ("-at", "-id")
    tab = True
    verbose_name_plural = "Tarix"


class OrderPaymentInline(ReadOnlyTabularInline):
    model = Payment
    fields = ("id", "provider", "status", "amount", "provider_txn_id", "confirmed_at", "created_at")
    ordering = ("-created_at", "-id")
    tab = True
    verbose_name_plural = "To‘lovlar"


class OrderShipmentInline(ReadOnlyTabularInline):
    model = Shipment
    fields = ("state", "tracking_number", "tariff", "bts_status", "via", "attempts", "last_attempt_at")
    tab = True
    verbose_name_plural = "Yetkazish"


class OrderCashbackHoldInline(ReadOnlyTabularInline):
    model = CashbackHold
    fk_name = "order"
    fields = ("amount", "state", "expires_at", "captured_at", "released_at", "release_reason")
    ordering = ("-id",)
    tab = True
    verbose_name_plural = "Cashback bandlari"


class OrderAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        customer_column(),
        status_column("status", "Holat", ORDER_STATUS_VARIANTS, ORDER_STATUS),
        "fulfilment",
        money_column("total", "Jami"),
        "payment_provider",
        "channel",
        "created_at",
    )
    list_filter = (
        dropdown("status"),
        dropdown("fulfilment"),
        dropdown("payment_provider"),
        dropdown("channel"),
        date_range("created_at"),
    )
    list_select_related = ("customer",)
    search_fields = ("=id", "customer__phone", "customer__name")
    ordering = ("-created_at", "-id")
    autocomplete_fields = ("customer", "cancelled_by")
    inlines = (
        OrderItemInline,
        OrderEventInline,
        OrderPaymentInline,
        OrderShipmentInline,
        OrderCashbackHoldInline,
    )
    date_hierarchy = "created_at"
    tab_groups = (
        (
            "Buyurtma",
            ("id", "customer", "channel", "fulfilment", "status", "payment_provider", "customer_note"),
        ),
        (
            "Summalar",
            ("items_subtotal", "promo_subtotal", "delivery_fee", "cashback_used", "total"),
        ),
        (
            "Manzil",
            (
                "recipient_name",
                "recipient_phone",
                "address_text",
                "address_region_code",
                "address_city_code",
                "address_comment",
            ),
        ),
        (
            "Vaqtlar",
            (
                "created_at",
                "updated_at",
                "reservation_minutes",
                "reservation_expires_at",
                "status_changed_at",
                "paid_at",
                "handed_to_courier_at",
                "delivered_at",
            ),
        ),
        ("Bekor qilish", ("cancelled_at", "cancellation_reason", "cancellation_note", "cancelled_by")),
    )


admin.site.register(Order, OrderAdmin)
register_read_only(
    OrderItem,
    list_display=(
        "order",
        "position",
        "name",
        "billz_id",
        "quantity",
        money_column("unit_price", "Narx"),
        "promo",
    ),
    search_fields=("=order__id", "billz_id", "name"),
    ordering=("-order_id", "position"),
    autocomplete_fields=("order", "product"),
)
register_read_only(
    OrderEvent,
    list_display=("id", "order", "at", "type", "source", "status_after", "actor"),
    list_filter=(dropdown("type"), dropdown("source"), date_range("at")),
    search_fields=("=order__id",),
    ordering=("-at", "-id"),
    autocomplete_fields=("order", "actor"),
    date_hierarchy="at",
)
register_read_only(
    StockReservation,
    list_display=(
        "id",
        "order",
        "product",
        "quantity",
        status_column("state", "Holat", RESERVATION_STATE_VARIANTS),
        "expires_at",
    ),
    list_filter=(dropdown("state"), dropdown("release_reason")),
    search_fields=("=order__id", "product__billz_id"),
    ordering=("-id",),
    autocomplete_fields=("order", "product"),
)
register_read_only(
    Cart,
    list_display=("id", customer_column(), "updated_at", "last_checkout_order"),
    list_select_related=("customer", "last_checkout_order"),
    search_fields=("customer__phone",),
    ordering=("-updated_at",),
    autocomplete_fields=("customer", "last_checkout_order"),
)
register_read_only(
    CartItem,
    list_display=("id", "cart", "product", "quantity", "added_at", "updated_at"),
    search_fields=("cart__customer__phone", "product__billz_id"),
    ordering=("cart_id", "added_at", "id"),
    autocomplete_fields=("cart", "product"),
)
