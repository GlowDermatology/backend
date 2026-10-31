"""Read-only Django admin for the delivery models (design 02 §6). Writes go through ``delivery.services``."""

from django.contrib import admin

from glowderma.core.admin import (
    DANGER,
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
from glowderma.delivery.models import Shipment, ShipmentEvent

SHIPMENT_STATE_VARIANTS = {"not_created": WARNING, "failed": DANGER, "created": SUCCESS}
SHIPMENT_EVENT_VARIANTS = {
    "created": SUCCESS,
    "create_failed": DANGER,
    "cancel_refused": WARNING,
}


class ShipmentEventInline(ReadOnlyTabularInline):
    model = ShipmentEvent
    fields = ("at", "kind", "bts_status", "via", "manual", "error_code")
    ordering = ("at", "id")
    tab = True
    verbose_name_plural = "Hodisalar"


class ShipmentAdmin(ReadOnlyAdmin):
    list_display = (
        "order",
        status_column("state", "Holat", SHIPMENT_STATE_VARIANTS),
        "tracking_number",
        money_column("tariff", "Tarif"),
        "bts_status",
        "via",
        "attempts",
        "last_attempt_at",
    )
    list_filter = (dropdown("state"), dropdown("via"), date_range("last_attempt_at"))
    search_fields = ("tracking_number", "=order__id")
    ordering = ("-id",)
    autocomplete_fields = ("order", "create_message")
    inlines = (ShipmentEventInline,)


admin.site.register(Shipment, ShipmentAdmin)
register_read_only(
    ShipmentEvent,
    list_display=(
        "at",
        "shipment",
        status_column("kind", "Hodisa", SHIPMENT_EVENT_VARIANTS),
        "bts_status",
        "via",
        "error_code",
    ),
    list_filter=(dropdown("kind"), dropdown("via"), date_range("at")),
    search_fields=("=shipment__id",),
    ordering=("-at", "-id"),
    autocomplete_fields=("shipment",),
    date_hierarchy="at",
)
