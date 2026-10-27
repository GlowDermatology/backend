"""factory-boy factory for the settings singleton: ``SiteSettingsFactory.create(...)`` returns pk 1."""

from typing import Any

from factory.django import DjangoModelFactory

from glowderma.site_config.models import SiteSettings


class SiteSettingsFactory(DjangoModelFactory[SiteSettings]):
    """The singleton comes from ``site_config.0002``; the factory updates and returns it (never a 2nd row)."""

    class Meta:
        model = SiteSettings

    reservation_minutes = 15
    support_phone = "+998712000000"
    pickup_address_uz = "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12"

    @classmethod
    def _create(cls, model_class: type[SiteSettings], *args: Any, **kwargs: Any) -> SiteSettings:
        kwargs.pop("id", None)
        row, _ = model_class.objects.update_or_create(pk=1, defaults=kwargs)
        return row
