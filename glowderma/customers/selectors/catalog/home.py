"""The storefront landing page (``GET home/``): the shop's numbers, the spotlight, the product rails and the
cashback tiers, in a bounded number of queries whatever the catalog size.

``feed_cursor`` is the ``catalog/feed/`` cursor right after the ``new`` rail's last product: the landing's
unfiltered feed starts there, so it never repeats the rail.

Queries: three rail id lists (newest, promo, the cached bestsellers still visible), the low-stock ids
(online selling only), one product read and one ``product_cards`` call (stock + images, + the cart when
signed in) over the union of every id, five counts and the active cashback policy (with its tiers). The
bestsellers are never computed here: ``commerce.refresh_bestsellers`` stores them hourly
(``commerce.selectors.cached_bestseller_ids``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db.models import Exists, OuterRef

from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import ProductImage
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, integration_modes, timeutils
from glowderma.customers.selectors.catalog import categories, feed, products
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.professionals import selectors as professional_selectors

RAIL_SIZE = 12
SPOTLIGHT_SIZE = 6
BESTSELLERS_RAIL_MIN = 4
LOW_STOCK_MIN = 1
LOW_STOCK_MAX = 5


def _percent(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def cashback_payload(now: datetime) -> dict[str, Any] | None:
    """The active policy's tiers and their percent range; ``None`` without an active policy (or tiers)."""
    rules = loyalty_selectors.rules_of(loyalty_selectors.active_policy(now))
    if rules is None or not rules.tiers:
        return None
    percents = [tier.percent for tier in rules.tiers]
    return {
        "min_percent": _percent(min(percents)),
        "max_percent": _percent(max(percents)),
        "tiers": [
            {"from_amount": tier.from_amount, "to_amount": tier.to_amount, "percent": _percent(tier.percent)}
            for tier in rules.tiers
        ],
    }


def _spotlight(
    sources: list[tuple[str, list[int]]], cards: dict[int, dict[str, Any]], hour: int
) -> list[dict[str, Any]]:
    """Up to ``SPOTLIGHT_SIZE`` products with an image, each once with its first reason, taken one per source
    in turn (sources in priority order) so every reason with products shows up, then rotated by
    ``hour mod length`` so the lead frame changes through the day."""
    chosen: list[dict[str, Any]] = []
    seen: set[int] = set()
    queues = [(reason, iter(ids)) for reason, ids in sources]
    while queues and len(chosen) < SPOTLIGHT_SIZE:
        for reason, ids in list(queues):
            if len(chosen) >= SPOTLIGHT_SIZE:
                break
            card = None
            for pk in ids:
                candidate = cards.get(pk)
                if candidate is not None and pk not in seen and candidate["main_image_url"] is not None:
                    seen.add(pk)
                    card = candidate
                    break
            if card is None:
                queues.remove((reason, ids))
                continue
            chosen.append({"reason": reason, "product": card})
    if not chosen:
        return chosen
    shift = hour % len(chosen)
    return chosen[shift:] + chosen[:shift]


def home_payload(lang: str, customer_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    instant = timeutils.now() if now is None else now
    online = integration_modes.online_sales_enabled()
    visible = catalog_selectors.customer_visible_products()
    newest = ("-created_at", "-id")
    newest_rows = list(visible.order_by(*newest).values_list("pk", "created_at")[:RAIL_SIZE])
    new_ids = [pk for pk, _ in newest_rows]
    # The feed continues after the `new` rail, so the landing never shows those products twice.
    feed_cursor = (
        feed.encode_cursor(feed.FeedCursor(created_at=newest_rows[-1][1], id=newest_rows[-1][0]))
        if newest_rows
        else None
    )
    promo_ids = list(visible.filter(promo=True).order_by(*newest).values_list("pk", flat=True)[:RAIL_SIZE])
    cached = commerce_selectors.cached_bestseller_ids()[: commerce_selectors.BESTSELLERS_LIMIT]
    still_visible = set(visible.filter(pk__in=cached).values_list("pk", flat=True)) if cached else set()
    bestseller_ids = [pk for pk in dict.fromkeys(cached) if pk in still_visible]
    low_stock_ids = (
        commerce_selectors.customer_low_stock_ids(
            visible.filter(Exists(ProductImage.objects.filter(product_id=OuterRef("pk")))),
            minimum=LOW_STOCK_MIN,
            maximum=LOW_STOCK_MAX,
            limit=SPOTLIGHT_SIZE,
        )
        if online
        else []
    )
    union = list(dict.fromkeys([*new_ids, *promo_ids, *bestseller_ids, *low_stock_ids]))
    rows = {row.pk: row for row in products.customer_products().filter(pk__in=union)} if union else {}
    cards = {
        card["id"]: card
        for card in products.product_cards([rows[pk] for pk in union if pk in rows], lang, customer_id)
    }

    def rail(ids: list[int]) -> list[dict[str, Any]]:
        return [cards[pk] for pk in ids if pk in cards]

    rails = [
        {"key": key, "items": items}
        for key, items in (
            (enums.HomeRailKey.NEW.value, rail(new_ids)),
            (enums.HomeRailKey.PROMO.value, rail(promo_ids)),
            (enums.HomeRailKey.BESTSELLERS.value, rail(bestseller_ids)),
        )
        if items and (key != enums.HomeRailKey.BESTSELLERS or len(items) >= BESTSELLERS_RAIL_MIN)
    ]
    spotlight = _spotlight(
        [
            (enums.HomeSpotlightReason.PROMO.value, promo_ids),
            (enums.HomeSpotlightReason.BESTSELLER.value, bestseller_ids),
            (enums.HomeSpotlightReason.LOW_STOCK.value, low_stock_ids),
            (enums.HomeSpotlightReason.NEW.value, new_ids),
        ],
        cards,
        instant.astimezone(timeutils.TASHKENT).hour,
    )
    return {
        "content_language": lang,
        "online_sales_enabled": online,
        "stats": {
            "products": visible.count(),
            "products_in_stock": commerce_selectors.customer_in_stock_count(visible),
            "categories": categories.active_category_count(),
            "specialists": professional_selectors.booking_catalogue().count(),
            "clinics": professional_selectors.active_locations().count(),
        },
        "spotlight": spotlight,
        "rails": rails,
        "cashback": cashback_payload(instant),
        "feed_cursor": feed_cursor,
    }


__all__ = [
    "BESTSELLERS_RAIL_MIN",
    "LOW_STOCK_MAX",
    "LOW_STOCK_MIN",
    "RAIL_SIZE",
    "SPOTLIGHT_SIZE",
    "cashback_payload",
    "home_payload",
]
