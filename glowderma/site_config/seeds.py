"""Demo seeder of the site_config module (design 03 §7.4): the singleton's demo values."""

from django.db import transaction

from glowderma.core.seeding import SeedContext, register_seeder
from glowderma.site_config.models import SiteSettings

SEED_VALUES = {
    "reservation_minutes": 15,
    "support_phone": "+998712000000",
    "pickup_address_uz": "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12",
    "pickup_address_ru": "",
    "pickup_address_en": "",
    "updated_by_id": None,
}


@register_seeder("site_config", order=30)
@transaction.atomic
def seed_site_config(context: SeedContext) -> None:
    settings_row = SiteSettings.load()
    changes = {name: value for name, value in SEED_VALUES.items() if getattr(settings_row, name) != value}
    if changes:
        SiteSettings.objects.filter(pk=settings_row.pk).update(**changes)
