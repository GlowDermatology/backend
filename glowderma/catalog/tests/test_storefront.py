"""``catalog.storefront``: every catalog write the website shows marks the website's cache tags stale, the
model saves and deletes (``watch``) and the ``QuerySet.update()`` / ``bulk_*`` writes of ``catalog.services``
alike."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.accounts.models import StaffUser
from glowderma.catalog import services
from glowderma.catalog.models import Banner, Category, ProductStock
from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.core import enums, timeutils
from glowderma.core.tests.storefront_support import revalidated

pytestmark = pytest.mark.django_db
PRODUCTS = {"shop:products"}
CATEGORIES = {"shop:categories", "shop:products"}
BANNERS = {"shop:banners"}
HOME = {"shop:home"}


def test_products_their_photos_and_stock(django_capture_on_commit_callbacks: Any) -> None:
    capture = django_capture_on_commit_callbacks
    product = ProductFactory.create()
    assert revalidated(capture, lambda: ProductFactory.create()) >= PRODUCTS

    def price() -> None:
        product.price = 199_000
        product.save(update_fields=["price"])

    assert revalidated(capture, price) == PRODUCTS
    image = ProductImageFactory.create(product=product)
    assert revalidated(capture, lambda: ProductImageFactory.create(product=product)) == PRODUCTS
    assert revalidated(capture, image.delete) == PRODUCTS
    assert revalidated(capture, lambda: ProductStockFactory.create(product=product, quantity=3)) == PRODUCTS


def test_categories_and_their_order(django_capture_on_commit_callbacks: Any, staff: StaffUser) -> None:
    capture = django_capture_on_commit_callbacks
    CategoryFactory.create_batch(2)
    assert revalidated(capture, lambda: CategoryFactory.create()) == CATEGORIES
    slugs = list(Category.objects.order_by("position", "id").values_list("slug", flat=True))
    reordered = [slugs[1], slugs[0], *slugs[2:]]
    assert revalidated(capture, lambda: services.reorder_categories(reordered, actor=staff)) == {
        "shop:categories"
    }


def test_deleting_a_category_unlinks_its_banners(
    django_capture_on_commit_callbacks: Any, staff: StaffUser
) -> None:
    category = CategoryFactory.create()
    BannerFactory.create(link_kind=enums.BannerLinkKind.CATEGORY, link_category=category)
    tags = revalidated(
        django_capture_on_commit_callbacks, lambda: services.delete_category(category, actor=staff)
    )
    assert tags >= CATEGORIES | BANNERS


def test_banners_and_their_order(django_capture_on_commit_callbacks: Any, staff: StaffUser) -> None:
    capture = django_capture_on_commit_callbacks
    first, second = BannerFactory.create_batch(2)
    assert revalidated(capture, lambda: BannerFactory.create()) == BANNERS
    order = list(Banner.objects.order_by("position", "id").values_list("pk", flat=True))
    order[0], order[1] = order[1], order[0]
    assert revalidated(capture, lambda: services.reorder_banners(order, actor=staff)) == BANNERS
    assert revalidated(capture, lambda: services.delete_banner(second, actor=staff)) == BANNERS
    assert first.pk


def test_a_photo_reorder_is_a_product_change(
    django_capture_on_commit_callbacks: Any, staff: StaffUser
) -> None:
    product = ProductFactory.create()
    images = [ProductImageFactory.create(product=product) for _ in range(2)]
    order = [images[1].pk, images[0].pk]
    tags = revalidated(
        django_capture_on_commit_callbacks, lambda: services.reorder_images(product.pk, order, actor=staff)
    )
    assert tags == PRODUCTS


def test_a_billz_stock_read_marks_products_only_when_the_quantity_moves(
    django_capture_on_commit_callbacks: Any,
) -> None:
    capture = django_capture_on_commit_callbacks
    product = ProductFactory.create()
    ProductStockFactory.create(product=product, quantity=5, synced_at=timeutils.now())
    later = timeutils.now()
    assert revalidated(capture, lambda: services.sync_stock(product, 5, later)) == set()
    assert revalidated(capture, lambda: services.sync_stock(product, 2, timeutils.now())) == PRODUCTS
    assert ProductStock.objects.get(product=product).quantity == 2


def test_the_landing_picks(django_capture_on_commit_callbacks: Any, staff: StaffUser) -> None:
    capture = django_capture_on_commit_callbacks
    product = ProductFactory.create()
    ProductImageFactory.create(product=product)
    ProductStockFactory.create(product=product, quantity=4)
    assert (
        revalidated(capture, lambda: services.set_home_picks("new", [str(product.pk)], actor=staff)) == HOME
    )
    assert revalidated(capture, lambda: services.set_home_picks("new", [], actor=staff)) == HOME
