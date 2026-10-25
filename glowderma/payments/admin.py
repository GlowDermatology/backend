"""Read-only Django admin for the payments models (design 02 §6). Writes go through ``payments.services``."""

from django.contrib import admin

from glowderma.core.admin import ReadOnlyAdmin, ReadOnlyTabularInline, register_read_only
from glowderma.payments.models import Payment, Refund


class RefundInline(ReadOnlyTabularInline):
    model = Refund
    fields = ("id", "state", "origin", "amount", "requested_at", "completed_at", "error_code", "requested_by")
    ordering = ("-requested_at", "-id")
    raw_id_fields = ("requested_by",)


class PaymentAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "order",
        "provider",
        "status",
        "amount",
        "provider_txn_id",
        "confirmed_at",
        "created_at",
    )
    list_filter = ("provider", "status")
    search_fields = ("provider_txn_id", "=order__id")
    ordering = ("-created_at", "-id")
    raw_id_fields = ("order",)
    inlines = (RefundInline,)
    date_hierarchy = "created_at"


admin.site.register(Payment, PaymentAdmin)
register_read_only(
    Refund,
    list_display=(
        "id",
        "payment",
        "state",
        "origin",
        "amount",
        "requested_at",
        "completed_at",
        "error_code",
    ),
    list_filter=("state", "origin"),
    search_fields=("provider_refund_id", "=payment__id"),
    ordering=("-requested_at", "-id"),
    raw_id_fields=("payment", "requested_by"),
)
