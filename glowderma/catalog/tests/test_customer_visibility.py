"""``catalog.selectors.customer_visible_products`` (customer contract §2.3 visibility)."""

import pytest

from glowderma.catalog import selectors
from glowderma.catalog.tests.factories import CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db


def test_visibility_needs_active_status_minimum_price_and_an_active_category() -> None:
    visible = ProductFactory.create(price=1000)
    ProductFactory.create(status="hidden")
    ProductFactory.create(price=999)
    ProductFactory.create(category=None)
    ProductFactory.create(category=CategoryFactory.create(is_active=False))
    assert list(selectors.customer_visible_products()) == [visible]
    assert selectors.CUSTOMER_MIN_PRICE == 1000
