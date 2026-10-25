"""Read-only Django admin for the appointments models (design 03 §8). Writes go through the services only."""

from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.core.admin import register_read_only

register_read_only(
    Appointment,
    list_display=("id", "professional", "customer", "start", "status", "requested_via", "price"),
    list_filter=("status", "professional", "requested_via"),
    search_fields=("customer__phone", "customer__name"),
    ordering=("-start", "-id"),
    raw_id_fields=("professional", "service", "customer", "created_by"),
)
register_read_only(
    AppointmentEvent,
    list_display=("id", "appointment", "at", "type", "source", "actor"),
    list_filter=("type", "source"),
    search_fields=("appointment__id",),
    ordering=("-at", "-id"),
    raw_id_fields=("appointment", "actor"),
)
