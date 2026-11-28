"""``get_storefront_cache()``: the live adapter when ``STOREFRONT_REVALIDATE_URL`` and
``STOREFRONT_REVALIDATE_SECRET`` are both set, the process-wide recorder otherwise (tests, local runs, and a
deployment that has not configured them yet: the website then refreshes on its cache lifetimes alone)."""

from __future__ import annotations

from django.conf import settings

from glowderma.integrations.storefront.fake import FakeStorefront
from glowderma.integrations.storefront.live import LiveStorefront
from glowderma.integrations.storefront.ports import StorefrontCache

_fake = FakeStorefront()


def configured() -> bool:
    return bool(settings.STOREFRONT_REVALIDATE_URL and settings.STOREFRONT_REVALIDATE_SECRET)


def get_storefront_cache() -> StorefrontCache:
    return LiveStorefront.from_settings() if configured() else _fake


def fake_storefront() -> FakeStorefront:
    return _fake


__all__ = [
    "configured",
    "fake_storefront",
    "get_storefront_cache",
]
