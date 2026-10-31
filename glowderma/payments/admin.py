"""Read-only Django admin for the payments models (design 02 §6). Writes go through ``payments.services``."""

from django.contrib import admin

from glowderma.core.admin import (
    DANGER,
    INFO,
    PRIMARY,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    date_range,
    dropdown,
    money_column,
    register_read_only,
    status_column,
)
from glowderma.core.labels import PAYMENT_STATUS, REFUND_STATE
from glowderma.payments.models import Payment, Refund

PAYMENT_STATUS_VARIANTS = {"pending": WARNING, "paid": SUCCESS, "failed": DANGER, "refunded": INFO}
REFUND_STATE_VARIANTS = {
    "requested": INFO,
    "submitting": PRIMARY,
    "succeeded": SUCCESS,
    "failed": DANGER,
    "manual_required": WARNING,
    "unknown": WARNING,
}


class RefundInline(ReadOnlyTabularInline):
    model = Refund
    fields = ("id", "state", "origin", "amount", "requested_at", "completed_at", "error_code", "requested_by")
    ordering = ("-requested_at", "-id")
    tab = True
    verbose_name_plural = "Qaytarishlar"


class PaymentAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "order",
        "provider",
        status_column("status", "Holat", PAYMENT_STATUS_VARIANTS, PAYMENT_STATUS),
        money_column("amount", "Summa"),
        "provider_txn_id",
        "confirmed_at",
        "created_at",
    )
    list_filter = (dropdown("provider"), dropdown("status"), date_range("created_at"))
    search_fields = ("provider_txn_id", "=order__id")
    ordering = ("-created_at", "-id")
    autocomplete_fields = ("order",)
    inlines = (RefundInline,)
    date_hierarchy = "created_at"


admin.site.register(Payment, PaymentAdmin)
register_read_only(
    Refund,
    list_display=(
        "id",
        "payment",
        status_column("state", "Holat", REFUND_STATE_VARIANTS, REFUND_STATE),
        "origin",
        money_column("amount", "Summa"),
        "requested_at",
        "completed_at",
        "error_code",
    ),
    list_filter=(dropdown("state"), dropdown("origin"), date_range("requested_at")),
    search_fields=("provider_refund_id", "=payment__id"),
    ordering=("-requested_at", "-id"),
    autocomplete_fields=("payment", "requested_by"),
)
