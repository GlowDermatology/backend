"""The commerce seams P0 adds for P1/P2 (plan §1 P0): cart lines, availability, stubs."""

import pytest
from django.test import override_settings

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.commerce import selectors, services, tasks
from glowderma.commerce.models import Cart, CartItem

pytestmark = pytest.mark.django_db


def test_delete_cart_lines_deletes_only_that_customers_lines() -> None:
    mine, other = CustomerFactory.create(), CustomerFactory.create()
    product = ProductFactory.create()
    for customer in (mine, other):
        cart = Cart.objects.create(customer=customer)
        CartItem.objects.create(cart=cart, product=product, quantity=2)
    CartItem.objects.create(cart=Cart.objects.get(customer=mine), product=ProductFactory.create(), quantity=1)
    assert services.delete_cart_lines(mine.pk) == 2
    assert not CartItem.objects.filter(cart__customer=mine).exists()
    assert CartItem.objects.filter(cart__customer=other).count() == 1
    assert Cart.objects.filter(customer=mine).exists()  # the cart row stays
    assert services.delete_cart_lines(CustomerFactory.create().pk) == 0


def test_customer_available_stock_clamps_and_follows_online_selling() -> None:
    product, oversold = ProductFactory.create(), ProductFactory.create()
    ProductStockFactory.create(product=product, quantity=3)
    ProductStockFactory.create(product=oversold, quantity=-2)
    assert selectors.available_stock([oversold.pk])[oversold.pk] == -2
    assert selectors.customer_available_stock([product.pk, oversold.pk]) == {product.pk: 3, oversold.pk: 0}
    with override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake"):
        assert selectors.customer_available_stock([product.pk]) == {product.pk: 0}
    with override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="live"):
        assert selectors.customer_available_stock([product.pk]) == {product.pk: 3}


def test_the_p0_seams_are_implemented() -> None:
    assert selectors.customer_inflight_order(CustomerFactory.create().pk) is None
    with pytest.raises(services.OrderNotFound):
        services.cancel_by_customer(10**9, 1)
    assert tasks.poll_bts() == []  # no candidate parcel
    assert tasks.poll_bts.name == "commerce.poll_bts"
