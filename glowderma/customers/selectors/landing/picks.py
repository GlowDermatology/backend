"""The landing sections staff filled by hand (admin «Bosh sahifa»), as the landing shows them now.

A pick that cannot show is skipped: a product that is hidden, has no photo (the spotlight) or is no longer on
promo (the promo rail); an inactive category; an unpublished specialist. A section left with nothing is
automatic again, so the landing never shows an empty block because a promotion ended.

Queries: the catalog picks and the specialist picks (one each), then one per section that has picks.
"""

from __future__ import annotations

from dataclasses import dataclass

from glowderma.catalog import selectors as catalog_selectors
from glowderma.professionals import selectors as professional_selectors


@dataclass(frozen=True, slots=True)
class LandingPicks:
    """The hand-filled sections that show now; a section that is absent (or ``None``) is automatic."""

    products: dict[str, list[int]]  # spotlight / new / promo / bestsellers → product ids in order
    categories: list[str] | None  # category slugs in order
    specialists: list[int] | None  # specialist ids in order


def landing_picks() -> LandingPicks:
    picks = catalog_selectors.home_picks()
    products: dict[str, list[int]] = {}
    for section, ids in picks.products.items():
        issues = catalog_selectors.product_pick_issues(section, ids)
        showing = [pk for pk in ids if pk in issues and issues[pk] is None]
        if showing:
            products[section] = showing
    categories: list[str] | None = None
    if picks.categories:
        category_issues = catalog_selectors.category_pick_issues(picks.categories)
        categories = [
            slug for slug in picks.categories if slug in category_issues and category_issues[slug] is None
        ]
    specialists: list[int] | None = None
    specialist_ids = professional_selectors.home_specialist_ids()
    if specialist_ids:
        specialist_issues = professional_selectors.specialist_pick_issues(specialist_ids)
        specialists = [
            pk for pk in specialist_ids if pk in specialist_issues and specialist_issues[pk] is None
        ]
    return LandingPicks(products=products, categories=categories or None, specialists=specialists or None)


__all__ = [
    "LandingPicks",
    "landing_picks",
]
