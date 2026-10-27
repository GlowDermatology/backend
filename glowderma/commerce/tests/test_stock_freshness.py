"""Stock freshness against Billz (gap X-STOCK-FRESHNESS, decision GC-5; customer contract §2.5, §2.7.1).

While Billz is live, ``POST cart/items/``, ``PATCH cart/items/{id}/`` and ``POST orders/checkout/`` first
re-read from Billz (recorded answers, no network) the stock of every line whose ``ProductStock.synced_at``
is older than ``BILLZ_STOCK_MAX_AGE_SECONDS`` — outside the request's transaction, so what was read is kept
whatever the answer. A checkout whose unread lines are older than ``BILLZ_STOCK_STALE_REFUSE_SECONDS``
answers 503; cart writes keep working from the cache. After a pushed sale the sold products are read again.
Fake Billz: nothing changes.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import services, tasks, topics
from glowderma.commerce.models import Cart, CartItem, Order, StockReservation
from glowderma.commerce.tests.support import pay, place, product, shopper
from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.billz.live import LiveBillz
from glowderma.integrations.tests.billz_support import BillzServer, Clock, fixture, live
from glowderma.loyalty.models import Purchase

pytestmark = pytest.mark.django_db
LIVE = {"BILLZ_MODE": "live"}
ITEMS = "/api/v1/cart/items/"
CHECKOUT = "/api/v1/orders/checkout/"
SERUM, FOAM = "4607012301", "4607012302"  # Billz (recorded): 12 and 0 in our shop
CREAM = "4607012303"  # Billz (recorded): listed, but no entry for our shop


def aged(item: Product, *, minutes: float | None) -> None:
    """Pretend Billz was last read ``minutes`` ago (``None``: never)."""
    synced = None if minutes is None else timeutils.now() - timedelta(minutes=minutes)
    ProductStock.objects.filter(product=item).update(synced_at=synced)


def stock(item: Product) -> ProductStock:
    return ProductStock.objects.get(product=item)


def down(server: BillzServer) -> None:
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(500, json=fixture("internal_error"))


def billz_searches(server: BillzServer) -> list[str]:
    return [r.url.params["search"] for r in server.requests if r.url.path == "/v2/products"]


def fill(customer: Customer, *lines: tuple[Product, int]) -> None:
    cart, _ = Cart.objects.get_or_create(customer=customer)
    for item, quantity in lines:
        CartItem.objects.create(cart=cart, product=item, quantity=quantity, price_at_add=item.price)


def checkout(client: APIClient, total: int) -> Any:
    body = {"fulfilment": "pickup", "address_id": None, "payment_provider": "click", "expected_total": total}
    return client.post(CHECKOUT, body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))


@pytest.fixture
def server() -> BillzServer:
    return BillzServer()


@pytest.fixture
def adapter(server: BillzServer) -> Any:
    port = live(server)
    with patch("glowderma.catalog.billz.get_port", return_value=port):
        yield port


# --- cart writes --------------------------------------------------------------------------------------------


@override_settings(**LIVE)
def test_cart_add_rereads_a_stale_line_before_clamping(
    customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    serum = product(185_000, billz_id=SERUM, stock=40)  # the cache says 40; Billz says 12
    aged(serum, minutes=20)
    before = timeutils.now()
    response = customer_client.post(
        ITEMS, {"product_id": serum.pk, "quantity": 9}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 200, response.json()
    assert billz_searches(server) == [SERUM]
    row = stock(serum)
    assert row.quantity == 12 and row.synced_at is not None and row.synced_at >= before
    line = response.json()["items"][0]
    assert (line["available"], line["max_quantity"], line["quantity"]) == (12, 10, 9)  # min(12, 10 per line)


@override_settings(**LIVE)
def test_a_fresh_line_is_not_read_again(
    customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    serum = product(185_000, billz_id=SERUM, stock=40)
    aged(serum, minutes=5)  # within BILLZ_STOCK_MAX_AGE_SECONDS (15 min)
    response = customer_client.post(
        ITEMS, {"product_id": serum.pk}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 200 and server.requests == []
    assert stock(serum).quantity == 40


@override_settings(**LIVE)
def test_cart_writes_keep_working_from_the_cache_while_billz_is_down(
    customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    down(server)
    serum = product(185_000, billz_id=SERUM, stock=3)
    aged(serum, minutes=24 * 60)  # far beyond the refusal bound: carts never refuse
    response = customer_client.post(
        ITEMS, {"product_id": serum.pk, "quantity": 2}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 200, response.json()
    assert response.json()["items"][0]["quantity"] == 2 and stock(serum).quantity == 3
    response = customer_client.patch(f"{ITEMS}{serum.pk}/", {"quantity": 3}, format="json")
    assert response.status_code == 200 and response.json()["items"][0]["quantity"] == 3


@override_settings(**LIVE)
def test_cart_patch_rereads_and_out_of_stock_follows_billz(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    foam = product(95_000, billz_id=FOAM, stock=5)  # Billz: 0
    fill(customer, (foam, 1))
    aged(foam, minutes=None)  # never read from Billz
    response = customer_client.patch(f"{ITEMS}{foam.pk}/", {"quantity": 2}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "OUT_OF_STOCK"
    assert billz_searches(server) == [FOAM] and stock(foam).quantity == 0  # kept despite the 409


@override_settings(**LIVE)
def test_a_barcode_billz_no_longer_knows_reads_as_zero(
    customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    gone = product(50_000, billz_id="4607099999", stock=8)
    aged(gone, minutes=60)
    response = customer_client.post(
        ITEMS, {"product_id": gone.pk}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 409 and response.json()["code"] == "OUT_OF_STOCK"
    assert stock(gone).quantity == 0


# --- checkout -----------------------------------------------------------------------------------------------


@override_settings(**LIVE)
def test_checkout_rereads_stale_lines_and_sells_on_the_fresh_stock(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    serum = product(185_000, billz_id=SERUM, stock=1)  # the cache says 1; Billz says 12
    foam = product(95_000, billz_id=FOAM, stock=4)  # the cache says 4; Billz says 0
    fill(customer, (serum, 2), (foam, 1))
    aged(serum, minutes=30)
    aged(foam, minutes=None)
    response = checkout(customer_client, 2 * 185_000 + 95_000)
    assert response.status_code == 409, response.json()
    body = response.json()
    assert body["code"] == "OUT_OF_STOCK"
    assert [(line["product_id"], line["status"]) for line in body["details"]["lines"]] == [
        (foam.pk, "out_of_stock")  # the serum line passes on Billz's 12, the foam fails on Billz's 0
    ]
    assert billz_searches(server) == [FOAM, SERUM]  # never read first, then the oldest
    assert (stock(serum).quantity, stock(foam).quantity) == (12, 0)  # kept despite the 409
    CartItem.objects.filter(product=foam).delete()
    server.requests.clear()
    response = checkout(customer_client, 2 * 185_000)
    assert response.status_code == 201, response.json()
    assert server.requests == []  # both lines were read a moment ago
    assert Order.objects.get().items.get().quantity == 2


@override_settings(**LIVE)
def test_checkout_refuses_while_billz_cannot_confirm_an_old_cache(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    down(server)
    serum = product(185_000, billz_id=SERUM, stock=5)
    fill(customer, (serum, 1))
    aged(serum, minutes=61)  # older than BILLZ_STOCK_STALE_REFUSE_SECONDS (1 h)
    response = checkout(customer_client, 185_000)
    assert response.status_code == 503, response.json()
    assert response.json()["code"] == "provider_unavailable"
    assert not Order.objects.exists() and CartItem.objects.filter(product=serum).exists()
    aged(serum, minutes=None)  # never read at all: refused too
    assert checkout(customer_client, 185_000).status_code == 503


@override_settings(**LIVE)
def test_checkout_proceeds_on_a_recent_cache_while_billz_is_down(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    down(server)
    serum = product(185_000, billz_id=SERUM, stock=5)
    fill(customer, (serum, 1))
    aged(serum, minutes=40)  # stale (> 15 min) but within the refusal bound (1 h)
    response = checkout(customer_client, 185_000)
    assert response.status_code == 201, response.json()
    assert billz_searches(server) == [SERUM, SERUM]  # tried (one retry), then the cache was used
    assert stock(serum).quantity == 5


@override_settings(**LIVE, BILLZ_STOCK_STALE_REFUSE_SECONDS=7200)
def test_the_refusal_bound_is_a_setting(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    down(server)
    serum = product(185_000, billz_id=SERUM, stock=5)
    fill(customer, (serum, 1))
    aged(serum, minutes=90)
    assert checkout(customer_client, 185_000).status_code == 201


@override_settings(**LIVE)
def test_a_partial_read_is_kept_when_the_checkout_is_refused(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    serum = product(185_000, billz_id=SERUM, stock=1)
    foam = product(95_000, billz_id=FOAM, stock=3)
    fill(customer, (serum, 1), (foam, 1))
    aged(serum, minutes=120)  # read first (the oldest)
    aged(foam, minutes=90)
    original = server._products

    def first_only(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("search") == FOAM:
            raise httpx.ConnectError("refused", request=request)
        return original(request)

    server.overrides["GET /v2/products"] = first_only
    response = checkout(customer_client, 185_000 + 95_000)
    assert response.status_code == 503
    serum_row = stock(serum)
    assert serum_row.quantity == 12 and serum_row.synced_at is not None
    assert serum_row.synced_at > timeutils.now() - timedelta(minutes=1)  # committed before the 503
    assert stock(foam).quantity == 3  # not read: untouched


@override_settings(**LIVE)
def test_a_slow_but_healthy_billz_does_not_refuse_the_checkout(
    customer: Customer, customer_client: APIClient, server: BillzServer
) -> None:
    """Review fix: the lines the 4 s budget did not reach are *deferred* (the cache stands), not refused."""
    clock = Clock()
    port = live(server, clock)
    serum = product(185_000, billz_id=SERUM, stock=1)  # Billz: 12
    foam = product(95_000, billz_id=FOAM, stock=4)
    cream = product(60_000, billz_id=CREAM, stock=3)
    fill(customer, (serum, 2), (foam, 1), (cream, 1))
    for item in (serum, foam, cream):
        aged(item, minutes=120)  # far beyond BILLZ_STOCK_STALE_REFUSE_SECONDS
    original = server._products

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("search") == SERUM:
            clock.now += 1.5
            return original(request)
        clock.now += 10  # the read timeout was capped by the deadline: the budget ran out
        raise httpx.ReadTimeout("slow", request=request)

    server.overrides["GET /v2/products"] = slow
    with patch("glowderma.catalog.billz.get_port", return_value=port):
        response = checkout(customer_client, 2 * 185_000 + 95_000 + 60_000)
    assert response.status_code == 201, response.json()
    assert billz_searches(server) == [SERUM, FOAM]  # the cream was never asked: no time left
    assert (stock(serum).quantity, stock(foam).quantity, stock(cream).quantity) == (12, 4, 3)


@override_settings(**LIVE)
def test_nothing_is_read_for_a_request_that_will_be_refused_or_replayed(
    customer: Customer, customer_client: APIClient, server: BillzServer, adapter: LiveBillz
) -> None:
    """Review fix: no Billz lookup for a hidden product, a missing / malformed key, an invalid body, a line
    that is not in the cart, or a replayed key (the Billz limiter and circuit are shared)."""
    hidden = product(95_000, billz_id=FOAM, stock=4, status="hidden")
    serum = product(185_000, billz_id=SERUM, stock=4)
    aged(hidden, minutes=None)
    aged(serum, minutes=None)

    def add(body: dict[str, Any], key: str | None) -> Any:
        if key is None:
            return customer_client.post(ITEMS, body, format="json")
        return customer_client.post(ITEMS, body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    assert add({"product_id": hidden.pk}, str(uuid.uuid4())).status_code == 409
    assert add({"product_id": serum.pk}, None).status_code == 400
    assert add({"product_id": serum.pk}, "not-a-uuid").status_code == 400
    assert add({"product_id": serum.pk, "quantity": 0}, str(uuid.uuid4())).status_code == 400
    assert customer_client.patch(f"{ITEMS}{serum.pk}/", {"quantity": 2}, format="json").status_code == 404
    assert customer_client.post(CHECKOUT, {"fulfilment": "pickup"}, format="json").status_code == 400
    assert billz_searches(server) == []
    # a replay answers from the stored response without asking Billz again
    replay = str(uuid.uuid4())
    assert add({"product_id": serum.pk}, replay).status_code == 200
    assert billz_searches(server) == [SERUM]
    aged(serum, minutes=None)
    response = add({"product_id": serum.pk}, replay)
    assert response.status_code == 200 and response.headers.get("Idempotent-Replayed") == "true"
    assert billz_searches(server) == [SERUM]


@pytest.mark.django_db(transaction=True)
@override_settings(**LIVE)
def test_billz_is_read_outside_every_transaction(
    customer: Customer, customer_client: APIClient, server: BillzServer
) -> None:
    """The brief: the re-read runs before ``@idempotent`` and every row lock (no transaction open)."""
    port = live(server)
    serum = product(185_000, billz_id=SERUM, stock=40)
    original = port.read_stock
    calls: list[bool] = []

    def outside(*args: Any, **kwargs: Any) -> Any:
        calls.append(connection.in_atomic_block)
        return original(*args, **kwargs)

    with (
        patch("glowderma.catalog.billz.get_port", return_value=port),
        patch.object(port, "read_stock", side_effect=outside),
    ):
        aged(serum, minutes=None)
        body = {"product_id": serum.pk}
        response = customer_client.post(ITEMS, body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        assert response.status_code == 200
        aged(serum, minutes=None)
        assert customer_client.patch(f"{ITEMS}{serum.pk}/", {"quantity": 2}, format="json").status_code == 200
        aged(serum, minutes=None)
        assert checkout(customer_client, 2 * 185_000).status_code == 201
    assert calls == [False, False, False]


def test_fake_billz_changes_nothing(customer: Customer, customer_client: APIClient) -> None:
    serum = product(185_000, billz_id=SERUM, stock=2)
    fill(customer, (serum, 1))
    aged(serum, minutes=None)
    with patch("glowderma.catalog.billz.get_port", side_effect=AssertionError("Billz must not be read")):
        assert (
            customer_client.post(
                ITEMS, {"product_id": serum.pk}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
            ).status_code
            == 200
        )
        assert checkout(customer_client, 2 * 185_000).status_code == 201
    assert stock(serum).synced_at is None


# --- the service --------------------------------------------------------------------------------------------


@override_settings(**LIVE)
def test_refresh_reports_what_it_could_not_read(server: BillzServer, adapter: LiveBillz) -> None:
    serum = product(185_000, billz_id=SERUM, stock=1)
    fresh = product(95_000, billz_id=FOAM, stock=1)
    aged(serum, minutes=30)
    aged(fresh, minutes=1)
    first = services.refresh_stale_stock([serum.pk, fresh.pk])
    assert first == services.StockCheck(frozenset({serum.pk}), {})
    down(server)
    aged(serum, minutes=30)
    synced_at = stock(serum).synced_at
    check = services.refresh_stale_stock([serum.pk, fresh.pk])
    assert check.refreshed == frozenset() and check.unread == {serum.pk: synced_at}
    assert not check.refuses_checkout(timeutils.now())
    assert check.refuses_checkout(timeutils.now() + timedelta(minutes=31))
    assert services.refresh_stale_stock([]) == services.FRESH


@override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake")
def test_no_reread_while_online_selling_is_off() -> None:
    serum = product(185_000, billz_id=SERUM, stock=1)
    with patch("glowderma.catalog.billz.get_port", side_effect=AssertionError("never")):
        assert services.refresh_stale_stock([serum.pk]) is services.FRESH


# --- after a pushed sale ------------------------------------------------------------------------------------


@override_settings(**LIVE)
def test_a_pushed_sale_enqueues_a_reread_that_settles_the_sold_units(server: BillzServer) -> None:
    serum = product(185_000, billz_id=SERUM, stock=13)
    order = pay(place(shopper(phone="+998901234567"), [(serum, 1)], fulfilment="pickup"))
    message = OutboxMessage.objects.get(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order.pk)
    with patch("glowderma.commerce.billz_sales.get_billz", return_value=live(server)):
        tasks.push_sale(message.payload)
    assert Purchase.objects.get(order=order).billz_pushed_at is not None
    reread = OutboxMessage.objects.get(topic=topics.BILLZ_STOCK_REREAD)
    assert reread.payload == {"order_id": order.pk, "product_ids": [serum.pk]}
    assert reread.dedupe_key == f"billz.stock_reread:{order.pk}"
    assert reread.available_at >= timeutils.now() + tasks.STOCK_REREAD_DELAY - timedelta(seconds=5)
    # Billz's stock was not read since the push: the sold unit is not settled yet
    assert StockReservation.objects.get(order=order).billz_settled_at is None
    server.requests.clear()
    with patch("glowderma.catalog.billz.get_port", return_value=live(server)):
        tasks.handle_billz_stock_reread(reread)
    assert billz_searches(server) == [SERUM]  # read whatever its age (batch profile)
    assert stock(serum).quantity == 12
    assert StockReservation.objects.get(order=order).billz_settled_at is not None
    # a redelivery reads again and settles nothing new; Billz down: best effort, no exception
    down(server)
    with patch("glowderma.catalog.billz.get_port", return_value=live(server)):
        tasks.handle_billz_stock_reread(reread)


def test_no_reread_after_a_fake_push() -> None:
    serum = product(185_000, billz_id=SERUM, stock=13)
    order = pay(place(shopper(phone="+998901234567"), [(serum, 1)], fulfilment="pickup"))
    message = OutboxMessage.objects.get(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order.pk)
    tasks.push_sale(message.payload)
    assert not OutboxMessage.objects.filter(topic=topics.BILLZ_STOCK_REREAD).exists()
