"""What the website shows from ``catalog`` (``core.storefront``): products (every list, page and the landing:
one tag), categories (their names are on product cards too), banners and the landing's hand-picked sections.

``connect`` watches the models' saves and deletes; the ``QuerySet.update()`` / ``bulk_*`` writes in
``catalog.services`` (orders, the Billz sync, the landing picks) call ``storefront.changed`` themselves."""

from __future__ import annotations

from glowderma.catalog.models import Banner, Category, HomePick, Product, ProductImage, ProductStock
from glowderma.core import storefront


def connect() -> None:
    storefront.watch(Product, [storefront.PRODUCTS])
    storefront.watch(ProductImage, [storefront.PRODUCTS])
    storefront.watch(ProductStock, [storefront.PRODUCTS])
    storefront.watch(Category, [storefront.CATEGORIES, storefront.PRODUCTS])
    storefront.watch(Banner, [storefront.BANNERS])
    storefront.watch(HomePick, [storefront.HOME])


__all__ = [
    "connect",
]
