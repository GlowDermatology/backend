"""The customer catalog's categories (customer contract §2.3)."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet

from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import Category
from glowderma.core import i18n
from glowderma.customers.selectors import common


def customer_categories() -> QuerySet[Category]:
    """Active categories by position; ``product_count`` = visible products."""
    visible = catalog_selectors.customer_visible_products()
    return (
        Category.objects.filter(is_active=True)
        .annotate(product_count=Count("products", filter=Q(products__in=visible), distinct=True))
        .order_by("position", "id")
    )


def active_category(slug: str) -> Category | None:
    return Category.objects.filter(slug=slug, is_active=True).first()


def category_payload(category: Category, lang: str) -> dict[str, Any]:
    return {
        "slug": category.slug,
        "name": common.localized_field(category, "name", lang),
        "content_language": i18n.content_language(category, "name", lang),
        **common.media_urls("image", category.image),
        "product_count": int(getattr(category, "product_count", 0)),
        "updated_at": category.updated_at,
        **common.seo_payload(category, lang),
    }


__all__ = [
    "active_category",
    "category_payload",
    "customer_categories",
]
