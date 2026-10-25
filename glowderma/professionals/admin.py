"""Django admin for the professionals models — read-only in this slice (design 01 §11, 03 §8).

The six editable registrations (``Location``, ``Professional``, ``Service``, ``ProfessionalService``,
``WorkingHours``, ``BlockedPeriod`` through ``professionals.services``) arrive with B6;
``WorkingHoursOverride`` stays read-only in M1 (open point S-3).
"""

from glowderma.core.admin import register_read_only
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

register_read_only(
    Location,
    list_display=("id", "name_uz", "address_uz", "phone", "is_active", "position"),
    list_filter=("is_active",),
    search_fields=("name_uz", "name_ru", "name_en", "address_uz"),
    ordering=("position", "id"),
)
register_read_only(
    Professional,
    list_display=("id", "name", "role", "location", "slot_step_min", "position", "is_active"),
    list_filter=("role", "is_active"),
    search_fields=("name",),
    ordering=("position", "name", "id"),
    raw_id_fields=("location",),
)
register_read_only(
    Service,
    list_display=(
        "id",
        "name_uz",
        "short_name_uz",
        "default_duration_min",
        "default_price",
        "position",
        "is_active",
    ),
    list_filter=("is_active",),
    search_fields=("name_uz", "name_ru", "name_en", "short_name_uz"),
    ordering=("position", "id"),
)
register_read_only(
    ProfessionalService,
    list_display=("id", "professional", "service", "price", "duration_min", "is_active"),
    list_filter=("is_active",),
    search_fields=("professional__name", "service__name_uz"),
    ordering=("professional_id", "service_id"),
    raw_id_fields=("professional", "service"),
)
register_read_only(
    WorkingHours,
    list_display=("id", "professional", "weekday", "start", "end", "break_start", "break_end"),
    list_filter=("weekday",),
    search_fields=("professional__name",),
    ordering=("professional_id", "weekday"),
    raw_id_fields=("professional",),
)
register_read_only(
    WorkingHoursOverride,
    list_display=("id", "professional", "date", "start", "end", "created_by"),
    search_fields=("professional__name",),
    ordering=("-date", "id"),
    raw_id_fields=("professional", "created_by"),
)
register_read_only(
    BlockedPeriod,
    list_display=("id", "professional", "start", "end", "kind", "label", "created_by"),
    list_filter=("kind",),
    search_fields=("professional__name", "label"),
    ordering=("-start", "id"),
    raw_id_fields=("professional", "created_by"),
)
