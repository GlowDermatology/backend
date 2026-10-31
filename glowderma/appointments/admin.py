"""Read-only Django admin for the appointments models (design 03 §8). Writes go through the services only."""

from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.core.admin import (
    DANGER,
    INFO,
    SUCCESS,
    WARNING,
    customer_column,
    date_range,
    dropdown,
    money_column,
    register_read_only,
    status_column,
)

APPOINTMENT_STATUS_VARIANTS = {
    "requested": WARNING,
    "confirmed": INFO,
    "rejected": DANGER,
    "attended": SUCCESS,
}

register_read_only(
    Appointment,
    list_display=(
        "id",
        "professional",
        customer_column(),
        "start",
        status_column("status", "Holat", APPOINTMENT_STATUS_VARIANTS),
        "requested_via",
        money_column("price", "Narx"),
    ),
    list_filter=(dropdown("status"), "professional", dropdown("requested_via"), date_range("start")),
    list_select_related=("professional", "customer"),
    search_fields=("customer__phone", "customer__name"),
    ordering=("-start", "-id"),
    autocomplete_fields=("professional", "service", "customer", "created_by"),
)
register_read_only(
    AppointmentEvent,
    list_display=("id", "appointment", "at", "type", "source", "actor"),
    list_filter=("type", "source"),
    search_fields=("=appointment__id",),
    ordering=("-at", "-id"),
    autocomplete_fields=("appointment", "actor"),
)
