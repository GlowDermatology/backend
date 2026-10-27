"""Available stock while a paid order's sale is not in Billz yet (review A M3: oversell).

Billz owns ``ProductStock.quantity``; a converted reservation must keep counting until its sale is in Billz
and the stock was read after it — otherwise the next sync of an unpushed (deferred or dead) sale offers the
unit the customer already bought.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from importlib import import_module
from io import StringIO
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.catalog import tasks as catalog_tasks
from glowderma.catalog import topics as catalog_topics
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import selectors, services, topics
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, StockReservation
from glowderma.commerce.tests.support import AT, admin, pay, place, product, shopper
from glowderma.core import integration_modes, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.integrations.tests.billz_support import BillzServer, fixture, live
from glowderma.integrations.tests.factories import JobFactory
from glowderma.loyalty.models import Purchase

pytestmark = pytest.mark.django_db
settle_step = import_module("glowderma.commerce.migrations.0007_settle_billz_reservations")


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


# --- NEW-2: a dead sale staff entered by hand; the settled marker ------------------------------------------


def sync(server: BillzServer, stock: int) -> None:
    """A delta import reading ``stock`` for the serum, then its ``catalog.stock_synced`` announcement."""
    page = fixture("products_page1")
    page["products"][0]["shop_measurement_values"][0]["active_measurement_value"] = stock
    server.product_pages = [page, fixture("products_page2")]
    job = JobFactory.create(params={"mode": "delta"})
    with patch("glowderma.catalog.billz.get_port", return_value=live(server)):
        catalog_tasks.run_billz_import(job.pk)
    job.refresh_from_db()
    assert job.state == "succeeded", job.error
    announced = OutboxMessage.objects.get(topic=catalog_topics.STOCK_SYNCED, payload__job_id=job.pk)
    OutboxMessage.objects.filter(pk=announced.pk).update(available_at=AT - timedelta(days=1))
    dispatch_outbox()
    announced.refresh_from_db()
    assert announced.status == "done", announced.last_error


def settled(order: Order) -> list[bool]:
    return [r.billz_settled_at is not None for r in StockReservation.objects.filter(order=order)]


def a_dead_sale(stock: int = 5) -> tuple[Order, Product, BillzServer]:
    """A paid order whose push is dead: Billz bills its own price (D1 check) → ``BillzCheckByHand``."""
    serum = product(150_000, billz_id="4607012301", stock=stock)
    order = pay(place(shopper(phone="+998901234567"), [(serum, 1)], fulfilment="pickup"))
    ProductStock.objects.filter(product=serum).update(synced_at=AT)
    server = BillzServer()
    original = server.apply_item
    server.apply_item = lambda sale_id, payload: original(  # type: ignore[method-assign]
        sale_id, {**payload, "use_free_price": False}
    )
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        assert dispatch(order).status == "dead"
    return order, serum, server


@override_settings(BILLZ_MODE="live")
def test_a_dead_sale_entered_by_hand_counts_once_after_billz_mark_recorded() -> None:
    """NEW-2 (probe test_defect_a_dead_sale_entered_by_hand_is_subtracted_twice_for_ever): Billz stock 5, the
    sale dies, staff enter it in Billz by hand (Billz: 4). Until they record it the unit keeps counting (the
    system cannot know); ``billz_mark_recorded`` records it, and from the next read on Billz's 4 is offered —
    once, for ever after."""
    order, serum, server = a_dead_sale()
    sync(server, 4)
    assert available(serum) == 3  # not recorded yet: counted twice (Billz's own sale and our reservation)
    staff = admin()
    out = StringIO()
    call_command("billz_mark_recorded", "--order", str(order.pk), "--actor", staff.email, stdout=out)
    assert f"recorded order {order.pk}" in out.getvalue()
    purchase = Purchase.objects.get(order=order)
    assert purchase.billz_pushed_at is not None and purchase.billz_sale_id  # the push's draft
    assert available(serum) == 3  # the last read is older than the record: still counted
    sync(server, 4)
    assert available(serum) == 4 and settled(order) == [True]
    for _ in range(2):
        sync(server, 4)
        assert available(serum) == 4
    entry = AuditEntry.objects.get(action="billz.sale_recorded_by_hand", entity_id=str(order.pk))
    assert entry.actor_id == staff.pk and entry.reason == "manage.py billz_mark_recorded"
    assert entry.after is not None and entry.before is not None
    assert entry.after["billz_sale_id"] == purchase.billz_sale_id and entry.before["billz_pushed_at"] is None
    assert OrderEvent.objects.filter(order=order, type="billz_sale_recorded").count() == 1
    message = OutboxMessage.objects.get(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order.pk)
    OutboxMessage.objects.filter(pk=message.pk).update(status="pending", attempts=0)
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        assert dispatch(order).status == "done"  # a replay skips the recorded purchase
    assert server.count("POST /v2/order-payment/{order_id}") == 0


@override_settings(BILLZ_MODE="live")
def test_billz_mark_recorded_refusals_and_dry_run() -> None:
    order, _, _ = a_dead_sale()
    staff = admin()

    def run(*args: str) -> str:
        out = StringIO()
        call_command("billz_mark_recorded", *args, stdout=out)
        return out.getvalue()

    with pytest.raises(CommandError, match="no active staff member"):
        run("--order", str(order.pk), "--actor", "nobody@glowderma.uz")
    with pytest.raises(CommandError, match="does not exist"):
        run("--order", "999999", "--actor", staff.email)
    with pytest.raises(CommandError, match="holds the Billz sale"):
        run("--order", str(order.pk), "--actor", staff.email, "--billz-sale-id", "another-sale")
    message = OutboxMessage.objects.get(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order.pk)
    OutboxMessage.objects.filter(pk=message.pk).update(status="in_flight")
    with pytest.raises(CommandError, match="running right now"):
        run("--order", str(order.pk), "--actor", staff.email)
    OutboxMessage.objects.filter(pk=message.pk).update(status="dead")
    assert "would record" in run("--order", str(order.pk), "--actor", staff.email, "--dry-run")
    assert Purchase.objects.get(order=order).billz_pushed_at is None  # dry run: nothing changed
    assert not AuditEntry.objects.filter(action="billz.sale_recorded_by_hand").exists()
    run("--order", str(order.pk), "--actor", staff.email.upper())
    with pytest.raises(CommandError, match="already recorded"):
        run("--order", str(order.pk), "--actor", staff.email)
    unpaid = place(shopper(phone="+998901234568"))
    with pytest.raises(CommandError, match="no online purchase"):
        run("--order", str(unpaid.pk), "--actor", staff.email)
    paid = pay(place(shopper(phone="+998901234569"), fulfilment="pickup"))
    services.cancel_order(paid.pk, "Mijoz so‘rovi bilan", staff.pk)
    with pytest.raises(CommandError, match="cancelled"):
        run("--order", str(paid.pk), "--actor", staff.email)


@override_settings(BILLZ_MODE="live")
def test_a_sale_without_a_billz_draft_needs_its_id_and_then_leaves_k1() -> None:
    """A push that died before Billz created a draft: recording it without the id staff entered would keep the
    order on K1 (a purchase without a Billz sale id), so it is refused; with the id the order leaves K1."""
    order = pay(place(shopper(phone="+998901234570"), fulfilment="pickup"))
    staff = admin()
    purchase = Purchase.objects.get(order=order)
    assert purchase.billz_sale_id == "" and purchase.billz_pushed_at is None
    paid_before = timeutils.now() + timedelta(days=1)
    assert order.pk in selectors.unrecorded_billz_sales_before(paid_before, limit=50).ids
    with pytest.raises(CommandError, match="has no Billz draft: pass --billz-sale-id"):
        call_command(
            "billz_mark_recorded", "--order", str(order.pk), "--actor", staff.email, stdout=StringIO()
        )
    sale_id = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    call_command(
        "billz_mark_recorded",
        "--order",
        str(order.pk),
        "--actor",
        staff.email,
        "--billz-sale-id",
        sale_id,
        stdout=StringIO(),
    )
    recorded = Purchase.objects.get(pk=purchase.pk)
    assert recorded.billz_sale_id == sale_id and recorded.billz_pushed_at is not None
    assert order.pk not in selectors.unrecorded_billz_sales_before(paid_before, limit=50).ids
    since = timeutils.now() - timedelta(days=1)
    assert order.pk not in selectors.unrecorded_billz_sales(since, paid_before, limit=50).ids


def test_settlement_stamps_only_what_never_counts_again() -> None:
    """NEW-2: ``settle_billz_reservations`` stamps a conversion once its sale is in Billz and the stock was
    read after it, or a cancelled order's that never reached Billz once read after the conversion."""
    pushed = paid_order()
    serum = Product.objects.get(billz_id="4607012301")
    unpushed = pay(place(shopper(phone="+998901234568"), [(serum, 1)], fulfilment="pickup"))
    cancelled = pay(place(shopper(phone="+998901234569"), [(serum, 1)], fulfilment="pickup"))
    attempted = pay(place(shopper(phone="+998901234560"), [(serum, 1)], fulfilment="pickup"))
    staff = admin()
    for order in (cancelled, attempted):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    now = timeutils.now()
    Purchase.objects.filter(order=pushed).update(billz_pushed_at=now, billz_sale_id="sale-1")
    Purchase.objects.filter(order=attempted).update(billz_payment_attempted_at=now, billz_sale_id="sale-2")
    ProductStock.objects.filter(product=serum).update(synced_at=now)  # read at the push: not after it
    assert services.settle_billz_reservations() == 1  # the cancel whose payment was never attempted
    assert settled(cancelled) == [True] and settled(attempted) == [False]
    assert settled(pushed) == [False] and settled(unpushed) == [False]
    ProductStock.objects.filter(product=serum).update(synced_at=now + timedelta(minutes=1))
    assert services.settle_billz_reservations([serum.pk]) == 1
    assert settled(pushed) == [True] and settled(unpushed) == [False] and settled(attempted) == [False]
    assert services.settle_billz_reservations() == 0  # idempotent
    ProductStock.objects.filter(product=serum).update(synced_at=None)
    assert services.settle_billz_reservations() == 0  # never read from Billz: nothing is final


@override_settings(BILLZ_MODE="fake")
def test_a_sale_not_pushed_under_a_fake_billz_in_production_is_settled_at_once() -> None:
    """NEW-2: with Billz fake under the production settings staff enter the sale by hand (§6.14); its units
    must never start counting at the first live stock read."""
    order = paid_order()
    with patch.object(integration_modes, "fake_in_production", return_value=True):
        assert dispatch(order).status == "done"
    assert Purchase.objects.get(order=order).billz_pushed_at is None and settled(order) == [True]


def test_the_data_migration_settles_what_the_previous_rule_no_longer_counted() -> None:
    """NEW-2, ``commerce.0007``: pushed and read after → settled; never read (the fake era) → settled; a
    cancelled order's unpushed sale read after the conversion → settled; an unpushed paid sale → counted."""
    serum = product(185_000, billz_id="4607012301", stock=5)
    never_read = product(95_000, billz_id="4607012302", stock=5)
    customers = iter(f"+99890123456{digit}" for digit in range(10))
    pushed = pay(place(shopper(phone=next(customers)), [(serum, 1)], fulfilment="pickup"))
    pushed_late = pay(place(shopper(phone=next(customers)), [(serum, 1)], fulfilment="pickup"))
    unpushed = pay(place(shopper(phone=next(customers)), [(serum, 1)], fulfilment="pickup"))
    cancelled = pay(place(shopper(phone=next(customers)), [(serum, 1)], fulfilment="pickup"))
    fake_era = pay(place(shopper(phone=next(customers)), [(never_read, 1)], fulfilment="pickup"))
    services.cancel_order(cancelled.pk, "Mijoz so‘rovi bilan", admin().pk)
    read = timeutils.now() + timedelta(hours=1)
    Purchase.objects.filter(order=pushed).update(billz_pushed_at=read - timedelta(minutes=5))
    Purchase.objects.filter(order=pushed_late).update(billz_pushed_at=read + timedelta(minutes=5))
    ProductStock.objects.filter(product=serum).update(synced_at=read)
    ProductStock.objects.filter(product=never_read).update(synced_at=None)
    StockReservation.objects.update(billz_settled_at=None)
    before = selectors.available_stock([serum.pk, never_read.pk])
    with connection.cursor() as cursor:
        cursor.execute(settle_step.SETTLE)
    assert settled(pushed) == [True] and settled(cancelled) == [True] and settled(fake_era) == [True]
    assert settled(pushed_late) == [False] and settled(unpushed) == [False]
    assert selectors.available_stock([serum.pk, never_read.pk]) == before  # the rule's answer is unchanged


# Query counts of the customer reads that compute availability, measured before NEW-2 (6aa053d): the settled
# marker must not add a query, whatever the length of the sales history.
PRODUCTS_QUERIES = 6
CART_QUERIES = 7
CHECKOUT_QUERIES = 45


def count_queries(call: Callable[[], Any]) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = call()
    assert response.status_code in (200, 201), response.content
    return len(captured.captured_queries)


def test_availability_reads_cost_the_same_with_a_long_sales_history(
    customer_client: APIClient, customer: Customer
) -> None:
    """NEW-2: GET catalog/products/, GET cart/ and POST orders/checkout/ keep their query counts with a long
    settled history and a few unsettled sales (the availability read stays one query)."""
    cream = product(312_000, billz_id="4607012301", stock=500)
    cart, _ = Cart.objects.get_or_create(customer=customer)
    CartItem.objects.create(cart=cart, product=cream, quantity=1, price_at_add=cream.price)

    def reads() -> tuple[int, int]:
        products = count_queries(lambda: customer_client.get("/api/v1/catalog/products/", {"page_size": 50}))
        return products, count_queries(lambda: customer_client.get("/api/v1/cart/"))

    customer_client.get("/api/v1/cart/")  # the session's first request also confirms the rotation
    empty = reads()
    sold = pay(place(shopper(phone="+998901234567"), [(cream, 1)], fulfilment="pickup"))
    pay(place(shopper(phone="+998901234568"), [(cream, 2)], fulfilment="pickup"))  # unpushed: counted
    long_ago = timeutils.now() - timedelta(days=300)
    Purchase.objects.filter(order=sold).update(billz_pushed_at=long_ago, billz_sale_id="sale-1")
    ProductStock.objects.filter(product=cream).update(synced_at=timeutils.now())
    StockReservation.objects.bulk_create(
        StockReservation(
            order_id=sold.pk,
            product_id=cream.pk,
            quantity=1,
            state=StockReservation.State.CONVERTED,
            expires_at=long_ago,
            converted_at=long_ago,
        )
        for _ in range(200)
    )
    assert services.settle_billz_reservations() == 201
    with CaptureQueriesContext(connection) as captured:
        assert selectors.available_stock([cream.pk]) == {cream.pk: 500 - 2}
    assert len(captured.captured_queries) == 1
    assert empty == reads() == (PRODUCTS_QUERIES, CART_QUERIES)
    checkout = count_queries(
        lambda: customer_client.post(
            "/api/v1/orders/checkout/",
            {
                "fulfilment": "pickup",
                "address_id": None,
                "payment_provider": "click",
                "expected_total": 312_000,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
    )
    assert checkout == CHECKOUT_QUERIES
