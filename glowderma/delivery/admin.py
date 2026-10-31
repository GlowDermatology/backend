"""Read-only Django admin for the delivery models (design 02 §6). Writes go through ``delivery.services``."""

from django.contrib import admin
from django.db import models

from glowderma.core.admin import (
    DANGER,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    date_range,
    display,
    dropdown,
    money_column,
    number_link_column,
    register_read_only,
    status_column,
)
from glowderma.delivery.models import Shipment, ShipmentEvent

SHIPMENT_STATE_VARIANTS = {"not_created": WARNING, "failed": DANGER, "created": SUCCESS}
# ``Shipment.bts_status`` / ``ShipmentEvent.bts_status``: the port's status vocabulary
# (glowderma.integrations.bts.protocol.port_status) — BTS's own codes are translated to these three, anything
# else is recorded as BTS sent it.
BTS_STATUS_LABELS = {
    "100": "Qabul qilindi (bekor qilish mumkin)",
    "200": "Kuryerda / yo‘lda",
    "300": "Yetkazildi",
    "1300": "BTS’da o‘chirilgan",
    "1400": "Muddati o‘tgan",
    "unknown": "Noma’lum holat",
}


def bts_status_text(value: str) -> str:
    """``Yetkazildi (300)``: the Uzbek meaning with the code BTS sent; ``-`` for none."""
    if not value:
        return "-"
    label = BTS_STATUS_LABELS.get(value)
    return f"{label} ({value})" if label and value != "unknown" else (label or value)


@display(description="BTS holati", ordering="bts_status")
def bts_status(obj: models.Model) -> str:
    return bts_status_text(str(getattr(obj, "bts_status", "")))


SHIPMENT_EVENT_VARIANTS = {
    "created": SUCCESS,
    "create_failed": DANGER,
    "cancel_refused": WARNING,
}


class ShipmentEventInline(ReadOnlyTabularInline):
    model = ShipmentEvent
    fields = ("at", "kind", "bts_status_label", "via", "manual", "error_code")
    bts_status_label = staticmethod(bts_status)
    ordering = ("at", "id")
    tab = True
    verbose_name_plural = "Hodisalar"


class ShipmentAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        number_link_column("order", "Buyurtma"),
        status_column("state", "Holat", SHIPMENT_STATE_VARIANTS),
        "tracking_number",
        money_column("tariff", "Tarif"),
        bts_status,
        "via",
        "attempts",
        "last_error_code",
        "last_attempt_at",
    )
    exclude = ("bts_status",)
    readonly_fields = ("bts_status_label",)
    bts_status_label = staticmethod(bts_status)
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
        number_link_column("shipment", "Jo‘natma", prefix="#"),
        status_column("kind", "Hodisa", SHIPMENT_EVENT_VARIANTS),
        bts_status,
        "via",
        "error_code",
    ),
    list_filter=(dropdown("kind"), dropdown("via"), date_range("at")),
    search_fields=("=shipment__id",),
    ordering=("-at", "-id"),
    exclude=("bts_status",),
    readonly_fields=("bts_status_label",),
    bts_status_label=staticmethod(bts_status),
    autocomplete_fields=("shipment",),
    date_hierarchy="at",
)
