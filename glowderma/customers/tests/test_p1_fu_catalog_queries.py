"""Follow-up of the P1 review: ``GET catalog/products/`` costs the same number of queries for 3 and for 15
products, signed in or not (customer contract §2.3). The review probe's 6 vs 9 compared a request that
already confirmed the session's rotation (``auth:sidrot`` flag set) with the session's first request, which
also runs the session read and the two once-per-rotation / throttled authentication UPDATEs."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glowderma.accounts import tokens
from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import start_test_session
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.commerce.tests.factories import CartFactory, CartItemFactory, StockReservationFactory
from glowderma.customers.tests.p1_support import app_client

pytestmark = pytest.mark.django_db
PRODUCTS = "/api/v1/catalog/products/"


def add_products(count: int) -> None:
    """``count`` visible products, each in its own category, with two images and a reservation."""
    for _ in range(count):
        product = ProductFactory.create(category=CategoryFactory.create())
        ProductStockFactory.create(product=product, quantity=5)
        ProductImageFactory.create(product=product)
        ProductImageFactory.create(product=product)
        StockReservationFactory.create(product=product, quantity=1)


def queries(client: Any, count: int) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(PRODUCTS, {"page_size": 50})
    assert response.status_code == 200, response.data
    assert len(response.data["results"]) == count
    return len(captured.captured_queries)


def costs(phone: str) -> dict[str, int]:
    """Query counts of the product list for every product present, anonymous and for a new signed-in
    customer holding a cart line of each product (its first request, then a second one)."""
    count = Product.objects.count()
    customer = Customer.objects.create(phone=phone, name="Katalog")
    cart = CartFactory.create(customer=customer)
    for product in Product.objects.all():
        CartItemFactory.create(cart=cart, product=product, quantity=2)
    access, _ = tokens.issue_access(start_test_session(customer).session)
    signed = app_client(fid="fid-test-0001", HTTP_AUTHORIZATION=f"Bearer {access}")
    return {
        "anonymous": queries(app_client(), count),
        "signed_first": queries(signed, count),
        "signed": queries(signed, count),
    }


def test_the_product_list_costs_no_query_per_product() -> None:
    add_products(3)
    small = costs("+998901010301")
    add_products(12)
    large = costs("+998901010315")
    assert small == large, (small, large)
