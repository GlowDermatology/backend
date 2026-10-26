"""Available stock while a paid order's sale is not in Billz yet (review A M3: oversell).

Billz owns ``ProductStock.quantity``; a converted reservation must keep counting until its sale is in Billz
and the stock was read after it — otherwise the next sync of an unpushed (deferred or dead) sale offers the
unit the customer already bought.
"""

from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings

from glowderma.catalog import tasks as catalog_tasks
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import selectors, services, topics
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import AT, admin, pay, place, product, shopper
from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.integrations.tests.billz_support import BillzServer, fixture, live
from glowderma.integrations.tests.factories import JobFactory
from glowderma.loyalty.models import Purchase

pytestmark = pytest.mark.django_db


def paid_order() -> Order:
    serum = product(185_000, billz_id="4607012301", stock=1)
    customer = shopper(phone="+998901234567")
    return pay(place(customer, [(serum, 1)], fulfilment="pickup"))


def dispatch(order: Order) -> OutboxMessage:
    message = OutboxMessage.objects.get(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order.pk)
    OutboxMessage.objects.filter(pk=message.pk).update(available_at=AT - timedelta(days=1))
    OutboxMessage.objects.exclude(pk=message.pk).update(status="done")
    dispatch_outbox()
    message.refresh_from_db()
    return message


def available(item: Product) -> int:
    return selectors.available_stock([item.pk])[item.pk]


@override_settings(BILLZ_MODE="live")
def test_an_unpushed_sale_stays_unavailable_after_the_next_sync() -> None:
    """The reviewer's probe (test_unpushed_sale_units_become_sellable_again_after_the_next_sync): the payment
    is refused (here a sale Billz never pays: 20999, dead), Billz still reports the unit, the daily sync
    refreshes ``synced_at`` — and the unit the customer bought must not be offered again."""
    order = paid_order()
    serum = Product.objects.get(billz_id="4607012301")
    ProductStock.objects.filter(product=serum).update(synced_at=AT)  # last Billz sync before the payment
    assert available(serum) == 0
    server = BillzServer()

    def refused(request: httpx.Request) -> httpx.Response:
        error = fixture("payment_error")
        error["data"].update(error_code=20999, message="payment refused")
        return httpx.Response(400, json=error)

    server.overrides["POST /v2/order-payment/{order_id}"] = refused
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        message = dispatch(order)
    assert message.status == "dead"
    page = fixture("products_page1")
    page["products"][0]["shop_measurement_values"][0]["active_measurement_value"] = 1  # Billz never sold it
    server.product_pages = [page, fixture("products_page2")]
    job = JobFactory.create(params={"mode": "delta"})
    with patch("glowderma.catalog.billz.get_port", return_value=live(server)):
        catalog_tasks.run_billz_import(job.pk)
    job.refresh_from_db()
    assert job.state == "succeeded", job.error
    synced_at = ProductStock.objects.get(product=serum).synced_at
    assert synced_at is not None and synced_at > AT  # the sync happened
    assert available(serum) == 0  # the unit the customer bought is still not offered


def test_a_pushed_sale_stops_counting_once_billz_was_read_after_it() -> None:
    order = paid_order()
    serum = Product.objects.get(billz_id="4607012301")
    purchase = Purchase.objects.get(order=order)
    stock = ProductStock.objects.get(product=serum)
    stock.synced_at = AT
    stock.save()
    pushed_at = timeutils.now()  # after the conversion (the order was paid at AT + 3 min)
    Purchase.objects.filter(pk=purchase.pk).update(billz_pushed_at=pushed_at)
    ProductStock.objects.filter(pk=stock.pk).update(synced_at=pushed_at)  # read at the push: still counts
    assert available(serum) == 0
    # Billz read after the push already reflects the sale (quantity 0): the reservation no longer counts
    ProductStock.objects.filter(pk=stock.pk).update(quantity=0, synced_at=pushed_at + timedelta(minutes=5))
    assert available(serum) == 0
    ProductStock.objects.filter(pk=stock.pk).update(quantity=1)  # a return in the store
    assert available(serum) == 1


def test_a_cancelled_unpushed_sale_stops_counting_after_the_next_sync() -> None:
    order = paid_order()
    serum = Product.objects.get(billz_id="4607012301")
    ProductStock.objects.filter(product=serum).update(synced_at=AT)
    services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)
    assert available(serum) == 0  # converted after the last read: still counted
    ProductStock.objects.filter(product=serum).update(synced_at=timeutils.now() + timedelta(hours=1))
    assert available(serum) == 1  # the unit never left Billz's stock


def test_converted_units_are_ignored_while_the_stock_was_never_read_from_billz() -> None:
    order = paid_order()
    serum = Product.objects.get(billz_id="4607012301")
    ProductStock.objects.filter(product=serum).update(synced_at=None)
    assert order.status == "processing" and available(serum) == 1
