"""What the website shows from ``catalog`` (``core.storefront``): products (every list, page and the landing:
one tag; and the categories' product counts and banner links), categories (their names are on product cards
too), banners and the landing's hand-picked sections.

``connect`` watches the models' saves and deletes; the ``QuerySet.update()`` / ``bulk_*`` writes in
``catalog.services`` (orders, the Billz sync, the landing picks) call ``storefront.changed`` themselves."""

from __future__ import annotations

from glowderma.catalog.models import Banner, Category, HomePick, Product, ProductImage, ProductStock
from glowderma.core import storefront

# What a product change can show: its cards and pages, its category's product count (a product's status,
# price or category decides whether it counts) and a banner that links to it (a hidden product's link is
# dropped).
PRODUCT_TAGS = (storefront.PRODUCTS, storefront.CATEGORIES, storefront.BANNERS)


def connect() -> None:
    storefront.watch(Product, PRODUCT_TAGS)
    storefront.watch(ProductImage, [storefront.PRODUCTS])
    storefront.watch(ProductStock, [storefront.PRODUCTS])
    # A category's name is on product cards; an inactive one hides its products and drops banner links.
    storefront.watch(Category, [storefront.CATEGORIES, storefront.PRODUCTS, storefront.BANNERS])
    storefront.watch(Banner, [storefront.BANNERS])
    storefront.watch(HomePick, [storefront.HOME])


__all__ = [
    "connect",
]
