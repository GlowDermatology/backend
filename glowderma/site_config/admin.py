"""Read-only Django admin for the settings singleton (design 01 §11); writes go through the admin API."""

from glowderma.core.admin import register_read_only
from glowderma.site_config.models import SiteSettings

register_read_only(
    SiteSettings,
    list_display=("id", "reservation_minutes", "support_phone", "updated_at", "updated_by"),
    autocomplete_fields=("updated_by",),
)
