"""The landing's automatic rules: the newest, promo, bestseller and low-stock product ids, and the spotlight's
pick order over them. A section staff did not fill by hand (``picks``) shows these.

Queries: three rail id lists (newest, promo, the cached bestsellers still visible) and the low-stock ids
(online selling only). The bestsellers are never computed here: ``commerce.refresh_bestsellers`` stores them
hourly (``commerce.selectors.cached_bestseller_ids``).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Exists, OuterRef, QuerySet

from glowderma.catalog.models import Product, ProductImage
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums

RAIL_SIZE = 12
SPOTLIGHT_SIZE = 6
BESTSELLERS_RAIL_MIN = 4
LOW_STOCK_MIN = 1
LOW_STOCK_MAX = 5
NEWEST = ("-created_at", "-id")


@dataclass(frozen=True, slots=True)
class AutomaticLists:
    """The automatic product id lists, each in its display order."""

    newest: list[tuple[int, datetime]]  # (id, created_at): the feed cursor continues after the last one
    promo: list[int]
    bestsellers: list[int]
    low_stock: list[int]

    @property
    def new(self) -> list[int]:
        return [pk for pk, _ in self.newest]

    def spotlight_sources(self) -> list[tuple[str, list[int]]]:
        """The spotlight's reasons in priority order, each with its products."""
        return [
            (enums.HomeSpotlightReason.PROMO.value, self.promo),
            (enums.HomeSpotlightReason.BESTSELLER.value, self.bestsellers),
            (enums.HomeSpotlightReason.LOW_STOCK.value, self.low_stock),
            (enums.HomeSpotlightReason.NEW.value, self.new),
        ]

    def reason_of(self, product_id: int, *, promo: bool) -> str | None:
        """The reason a hand-picked spotlight frame shows: the first true one, ``None`` when none is."""
        if promo:
            return enums.HomeSpotlightReason.PROMO.value
        for reason, ids in self.spotlight_sources()[1:]:
            if product_id in ids:
                return reason
        return None


def automatic_lists(visible: QuerySet[Product], *, online: bool) -> AutomaticLists:
    newest = list(visible.order_by(*NEWEST).values_list("pk", "created_at")[:RAIL_SIZE])
    promo = list(visible.filter(promo=True).order_by(*NEWEST).values_list("pk", flat=True)[:RAIL_SIZE])
    cached = commerce_selectors.cached_bestseller_ids()[: commerce_selectors.BESTSELLERS_LIMIT]
    still_visible = set(visible.filter(pk__in=cached).values_list("pk", flat=True)) if cached else set()
    low_stock = (
        commerce_selectors.customer_low_stock_ids(
            visible.filter(Exists(ProductImage.objects.filter(product_id=OuterRef("pk")))),
            minimum=LOW_STOCK_MIN,
            maximum=LOW_STOCK_MAX,
            limit=SPOTLIGHT_SIZE,
        )
        if online
        else []
    )
    return AutomaticLists(
        newest=newest,
        promo=promo,
        bestsellers=[pk for pk in dict.fromkeys(cached) if pk in still_visible],
        low_stock=low_stock,
    )


def spotlight_order(
    sources: list[tuple[str, list[int]]], with_image: Collection[int], hour: int
) -> list[tuple[str, int]]:
    """Up to ``SPOTLIGHT_SIZE`` ``(reason, product id)`` frames of products with an image, each once with its
    first reason, taken one per source in turn (sources in priority order) so every reason with products
    shows up, then rotated by ``hour mod length`` so the lead frame changes through the day."""
    chosen: list[tuple[str, int]] = []
    seen: set[int] = set()
    queues = [(reason, iter(ids)) for reason, ids in sources]
    while queues and len(chosen) < SPOTLIGHT_SIZE:
        for reason, ids in list(queues):
            if len(chosen) >= SPOTLIGHT_SIZE:
                break
            pick = next((pk for pk in ids if pk not in seen and pk in with_image), None)
            if pick is None:
                queues.remove((reason, ids))
                continue
            seen.add(pick)
            chosen.append((reason, pick))
    if not chosen:
        return chosen
    shift = hour % len(chosen)
    return chosen[shift:] + chosen[:shift]


__all__ = [
    "BESTSELLERS_RAIL_MIN",
    "LOW_STOCK_MAX",
    "LOW_STOCK_MIN",
    "RAIL_SIZE",
    "SPOTLIGHT_SIZE",
    "AutomaticLists",
    "automatic_lists",
    "spotlight_order",
]
