"""Read-only Django admin for the delivery models (design 02 §6). Writes go through ``delivery.services``."""

from django.contrib import admin

from glowderma.core.admin import ReadOnlyAdmin, ReadOnlyTabularInline, register_read_only
from glowderma.delivery.models import Shipment, ShipmentEvent


class ShipmentEventInline(ReadOnlyTabularInline):
    model = ShipmentEvent
    fields = ("at", "kind", "bts_status", "via", "manual", "error_code")
    ordering = ("at", "id")


class ShipmentAdmin(ReadOnlyAdmin):
    list_display = (
        "order",
        "state",
        "tracking_number",
        "tariff",
        "bts_status",
        "via",
        "attempts",
        "last_attempt_at",
    )
    list_filter = ("state", "via")
    search_fields = ("tracking_number", "=order__id")
    ordering = ("-id",)
    raw_id_fields = ("order", "create_message")
    inlines = (ShipmentEventInline,)


admin.site.register(Shipment, ShipmentAdmin)
register_read_only(
    ShipmentEvent,
    list_display=("at", "shipment", "kind", "bts_status", "via", "error_code"),
    list_filter=("kind", "via"),
    search_fields=("=shipment__id",),
    ordering=("-at", "-id"),
    raw_id_fields=("shipment",),
    date_hierarchy="at",
)
