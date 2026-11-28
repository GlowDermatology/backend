"""What the website shows from ``loyalty`` (``core.storefront``): the cashback policy (the landing's tiers
and each card's estimate). A new version saved for a later date takes effect with time alone: the landing
picks it up on its cache lifetime then."""

from __future__ import annotations

from glowderma.core import storefront
from glowderma.loyalty.models import CashbackPolicy, CashbackTier


def connect() -> None:
    storefront.watch(CashbackPolicy, [storefront.HOME])
    storefront.watch(CashbackTier, [storefront.HOME])


__all__ = [
    "connect",
]
