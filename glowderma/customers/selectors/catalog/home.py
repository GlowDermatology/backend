"""The storefront landing page (``GET home/``): the shop's numbers, the spotlight, the product rails, the
categories and specialists staff picked, and the cashback tiers, in a bounded number of queries whatever the
catalog size.

Each section is automatic (``landing.automatic``) unless staff filled it by hand (``landing.picks``, admin
«Bosh sahifa»): a hand-filled spotlight keeps its order (no hourly rotation), and a hand-filled
``bestsellers`` rail shows with fewer than ``BESTSELLERS_RAIL_MIN`` products. ``categories`` and
``specialists`` are the hand-picked slugs and ids, ``null`` when the section is automatic (the landing then
lists every active category and every listed specialist, as before).

``feed_cursor`` is the ``catalog/feed/`` cursor right after the automatic ``new`` rail's last product: the
landing's unfiltered feed starts there, so it never repeats the rail. A hand-filled ``new`` rail is not the
newest products, so the feed then starts at the newest (``null``).

Queries: the automatic lists (``landing.automatic``), the picks (``landing.picks``), one product read and one
``product_cards`` call (stock + images, + the cart when signed in) over the union of every id, five counts
and the active cashback policy (with its tiers).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from glowderma.catalog import selectors as catalog_selectors
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, integration_modes, timeutils
from glowderma.customers.selectors.catalog import categories, feed, products
from glowderma.customers.selectors.landing import automatic, picks
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.professionals import selectors as professional_selectors

RAIL_KEYS = (enums.HomeRailKey.NEW.value, enums.HomeRailKey.PROMO.value, enums.HomeRailKey.BESTSELLERS.value)


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


def home_payload(lang: str, customer_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    instant = timeutils.now() if now is None else now
    online = integration_modes.online_sales_enabled()
    visible = catalog_selectors.customer_visible_products()
    lists = automatic.automatic_lists(visible, online=online)
    chosen = picks.landing_picks()
    automatic_rails = {
        enums.HomeRailKey.NEW.value: lists.new,
        enums.HomeRailKey.PROMO.value: lists.promo,
        enums.HomeRailKey.BESTSELLERS.value: lists.bestsellers,
    }
    rail_ids = {key: chosen.products.get(key, automatic_rails[key]) for key in RAIL_KEYS}
    manual_spotlight = chosen.products.get(enums.HomeSectionKey.SPOTLIGHT)
    # The feed continues after the automatic `new` rail, so the landing never shows those products twice.
    feed_cursor = (
        feed.encode_cursor(feed.FeedCursor(created_at=lists.newest[-1][1], id=lists.newest[-1][0]))
        if lists.newest and enums.HomeRailKey.NEW.value not in chosen.products
        else None
    )
    union = list(
        dict.fromkeys(
            [*lists.new, *lists.promo, *lists.bestsellers, *lists.low_stock, *(manual_spotlight or [])]
            + [pk for key in RAIL_KEYS for pk in rail_ids[key]]
        )
    )
    rows = {row.pk: row for row in products.customer_products().filter(pk__in=union)} if union else {}
    cards = {
        card["id"]: card
        for card in products.product_cards([rows[pk] for pk in union if pk in rows], lang, customer_id)
    }

    def rail(ids: list[int]) -> list[dict[str, Any]]:
        return [cards[pk] for pk in ids if pk in cards]

    rails = [
        {"key": key, "items": items}
        for key in RAIL_KEYS
        if (items := rail(rail_ids[key]))
        and (
            key != enums.HomeRailKey.BESTSELLERS
            or key in chosen.products
            or len(items) >= automatic.BESTSELLERS_RAIL_MIN
        )
    ]
    if manual_spotlight is not None:
        spotlight = [
            {"reason": lists.reason_of(pk, promo=cards[pk]["promo"]), "product": cards[pk]}
            for pk in manual_spotlight
            if pk in cards and cards[pk]["main_image_url"] is not None
        ]
    else:
        with_image = {pk for pk, card in cards.items() if card["main_image_url"] is not None}
        frames = automatic.spotlight_order(
            lists.spotlight_sources(), with_image, instant.astimezone(timeutils.TASHKENT).hour
        )
        spotlight = [{"reason": reason, "product": cards[pk]} for reason, pk in frames]
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
        "categories": chosen.categories,
        "specialists": chosen.specialists,
        "cashback": cashback_payload(instant),
        "feed_cursor": feed_cursor,
    }


__all__ = [
    "RAIL_KEYS",
    "cashback_payload",
    "home_payload",
]
