"""What the website shows from ``site_config`` (``core.storefront``): the settings (contacts, delivery, the
ticker, the landing's numbers)."""

from __future__ import annotations

from glowderma.core import storefront
from glowderma.site_config.models import SiteSettings


def connect() -> None:
    storefront.watch(SiteSettings, [storefront.SETTINGS])


__all__ = [
    "connect",
]
