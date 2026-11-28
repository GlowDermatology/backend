"""The admin «Bosh sahifa» (admin contract §2.21): every landing section staff can fill by hand, with its
saved picks (each with the reason it does not show now, if any) and what the automatic rule shows right now,
so the page can offer «Qo‘lda tahrirlash» from the current automatic choice.

An item is ``{id, name, image_url, issue}``: ``id`` is the product or specialist id as a string, or the
category slug, which is what ``PUT home-sections/{key}/`` takes back. Names are the Uzbek ones (the admin
API's language).

Queries: the automatic lists, the picks and the issues of each hand-filled section, then one read per kind of
item shown (products with their first image, categories, specialists).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import Exists, OuterRef, Prefetch

from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import HOME_PICK_LIMITS, Category, Product, ProductImage
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.media import optional_variant_url
from glowderma.customers.selectors.landing import automatic
from glowderma.professionals import selectors as professional_selectors
from glowderma.professionals.models import HOME_SPECIALISTS_LIMIT, Professional

PRODUCT_SECTIONS = (
    enums.HomeSectionKey.SPOTLIGHT,
    enums.HomeSectionKey.NEW,
    enums.HomeSectionKey.PROMO,
    enums.HomeSectionKey.BESTSELLERS,
)


def _limit(key: str) -> int:
    if key == enums.HomeSectionKey.SPECIALISTS:
        return HOME_SPECIALISTS_LIMIT
    return HOME_PICK_LIMITS[key]


def _item(ref: str, name: str, image: str | None, issue: str | None) -> dict[str, Any]:
    return {"id": ref, "name": name, "image_url": optional_variant_url(image, "thumbnail"), "issue": issue}


def _products(ids: Iterable[int]) -> dict[int, tuple[str, str | None]]:
    """``id → (name, first image file)`` for any product, shown or not."""
    first = Prefetch("images", queryset=ProductImage.objects.order_by("position", "id"), to_attr="ordered")
    rows = Product.objects.filter(pk__in=set(ids)).prefetch_related(first)
    return {
        row.pk: (row.name_uz or row.billz_name, row.ordered[0].file.name if row.ordered else None)
        for row in rows
    }


def _automatic_ids(lists: automatic.AutomaticLists, key: str) -> list[int]:
    if key == enums.HomeSectionKey.SPOTLIGHT:
        candidates = {pk for _, ids in lists.spotlight_sources() for pk in ids}
        with_image = set(
            Product.objects.filter(pk__in=candidates)
            .filter(Exists(ProductImage.objects.filter(product_id=OuterRef("pk"))))
            .values_list("pk", flat=True)
        )
        hour = timeutils.now().astimezone(timeutils.TASHKENT).hour
        return [pk for _, pk in automatic.spotlight_order(lists.spotlight_sources(), with_image, hour)]
    if key == enums.HomeSectionKey.NEW:
        return lists.new
    if key == enums.HomeSectionKey.PROMO:
        return lists.promo
    bestsellers = lists.bestsellers
    return bestsellers if len(bestsellers) >= automatic.BESTSELLERS_RAIL_MIN else []


def _product_sections() -> list[dict[str, Any]]:
    visible = catalog_selectors.customer_visible_products()
    lists = automatic.automatic_lists(visible, online=integration_modes.online_sales_enabled())
    saved = catalog_selectors.home_picks().products
    automatic_ids = {key: _automatic_ids(lists, key) for key in PRODUCT_SECTIONS}
    issues = {
        key: catalog_selectors.product_pick_issues(key, saved[key])
        for key in PRODUCT_SECTIONS
        if key in saved
    }
    known = _products(pk for key in PRODUCT_SECTIONS for pk in [*saved.get(key, []), *automatic_ids[key]])

    def items(ids: list[int], section_issues: dict[int, str | None] | None) -> list[dict[str, Any]]:
        """``section_issues`` of saved picks; ``None`` for the automatic choice (it shows by definition)."""
        return [
            _item(str(pk), *known[pk], None if section_issues is None else section_issues.get(pk))
            for pk in ids
            if pk in known
        ]

    return [
        {
            "key": key,
            "manual": key in saved,
            "limit": _limit(key),
            "picks": items(saved.get(key, []), issues.get(key, {})),
            "automatic": items(automatic_ids[key], None),
        }
        for key in PRODUCT_SECTIONS
    ]


def _categories_section() -> dict[str, Any]:
    saved = catalog_selectors.home_picks().categories
    issues = catalog_selectors.category_pick_issues(saved)
    rows = {
        row.slug: row
        for row in Category.objects.filter(slug__in=saved) | Category.objects.filter(is_active=True)
    }
    shown = [
        row.slug for row in sorted(rows.values(), key=lambda row: (row.position, row.pk)) if row.is_active
    ]

    def item(slug: str, issue: str | None) -> dict[str, Any]:
        row = rows[slug]
        return _item(slug, row.name_uz, row.image.name or None, issue)

    return {
        "key": enums.HomeSectionKey.CATEGORIES.value,
        "manual": bool(saved),
        "limit": _limit(enums.HomeSectionKey.CATEGORIES),
        "picks": [item(slug, issues.get(slug)) for slug in saved if slug in rows],
        "automatic": [item(slug, None) for slug in shown],
    }


def _specialists_section() -> dict[str, Any]:
    saved = professional_selectors.home_specialist_ids()
    issues = professional_selectors.specialist_pick_issues(saved)
    listed = list(professional_selectors.booking_catalogue().values_list("pk", flat=True))
    rows = {row.pk: row for row in Professional.objects.filter(pk__in={*saved, *listed})}

    def item(pk: int, issue: str | None) -> dict[str, Any]:
        row = rows[pk]
        return _item(str(pk), row.name, row.photo.name or None, issue)

    return {
        "key": enums.HomeSectionKey.SPECIALISTS.value,
        "manual": bool(saved),
        "limit": _limit(enums.HomeSectionKey.SPECIALISTS),
        "picks": [item(pk, issues.get(pk)) for pk in saved if pk in rows],
        "automatic": [item(pk, None) for pk in listed if pk in rows],
    }


def home_sections() -> list[dict[str, Any]]:
    """Every section in the landing's order: the spotlight, the rails, the categories, the specialists."""
    return [*_product_sections(), _categories_section(), _specialists_section()]


def home_section(key: str) -> dict[str, Any]:
    """One section (after a save)."""
    if key == enums.HomeSectionKey.CATEGORIES:
        return _categories_section()
    if key == enums.HomeSectionKey.SPECIALISTS:
        return _specialists_section()
    return next(section for section in _product_sections() if section["key"] == key)


__all__ = [
    "PRODUCT_SECTIONS",
    "home_section",
    "home_sections",
]
