"""What the website shows from ``commerce`` (``core.storefront``): a stock reservation (a checkout) changes
what a product has available, and the hourly bestsellers feed the landing's rail and carousel.

``connect`` watches the reservations' saves and deletes; ``commerce.services`` calls ``storefront.changed``
where it moves reservations with ``QuerySet.update()`` (paid, released, cancelled) and the bestsellers task
after it stores the new list."""

from __future__ import annotations

from glowderma.commerce.models import StockReservation
from glowderma.core import storefront


def connect() -> None:
    storefront.watch(StockReservation, [storefront.PRODUCTS])


__all__ = [
    "connect",
]
