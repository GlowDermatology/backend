"""Races of the customer API under real transactions (plan §3 P2 tests): checkouts for the last unit and of
one
customer, checkout vs a delete and vs a capture, pay switch vs Complete, duplicate and superseding Click
callbacks, concurrent Payme creates, expiry vs a late Complete."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.db import connection, transaction
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import services
from glowderma.commerce.models import Cart, CartItem, Order
from glowderma.commerce.tests.support import credit, place, product, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import (
    CLICK_COMPLETE,
    FORM,
    TXN,
    complete_params,
    payme,
    prepare,
    real_now,
    ref,
    tiyin,
)
from glowderma.core.api.exceptions import AlreadyPaid, AuthFailed, OpenOrderExists, PaymentInProgress
from glowderma.core.seeding import pinned_clock
from glowderma.integrations.payme import sandbox
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import Purchase
from glowderma.payments.models import Payment

pytestmark = pytest.mark.django_db(transaction=True)
TIMEOUT = 10


def _race(*calls: Callable[[], Any]) -> list[Any]:
    start = threading.Event()
    results: list[Any] = [None] * len(calls)

    def worker(index: int, call: Callable[[], Any]) -> None:
        try:
            start.wait(TIMEOUT)
            results[index] = call()
        except Exception as exc:  # noqa: BLE001 - asserted by the test
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(TIMEOUT)
    return results


def _cart(customer: Customer, *lines: tuple[Any, int]) -> None:
    cart, _ = Cart.objects.get_or_create(customer=customer)
    for item, quantity in lines:
        CartItem.objects.create(cart=cart, product=item, quantity=quantity, price_at_add=item.price)


def _input(total: int, **over: Any) -> services.CheckoutInput:
    values: dict[str, Any] = {
        "fulfilment": "pickup",
        "payment_provider": "click",
        "channel": "ios",
        "expected_total": total,
    }
    values.update(over)
    return services.CheckoutInput(**values)


def _post_form(url: str, params: dict[str, str]) -> dict[str, Any]:
    from urllib.parse import urlencode

    return dict(APIClient().generic("POST", url, urlencode(params), content_type=FORM).json())


def test_two_checkouts_for_the_last_unit() -> None:
    item = product(stock=1)
    first, second = shopper(), shopper()
    _cart(first, (item, 1))
    _cart(second, (item, 1))
    results = _race(
        lambda: services.checkout(first.pk, _input(item.price)),
        lambda: services.checkout(second.pk, _input(item.price)),
    )
    winners = [r for r in results if isinstance(r, int)]
    losers = [r for r in results if isinstance(r, services.OutOfStock)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0].lines[0]["available"] == 0 and losers[0].lines[0]["status"] == "out_of_stock"
    assert Order.objects.count() == 1


def test_two_checkouts_of_one_customer() -> None:
    customer = shopper()
    _cart(customer, (product(stock=5), 1))
    results = _race(
        lambda: services.checkout(customer.pk, _input(100_000)),
        lambda: services.checkout(customer.pk, _input(100_000)),
    )
    assert sum(isinstance(r, int) for r in results) == 1
    assert sum(isinstance(r, OpenOrderExists) for r in results) == 1, results
    assert Order.objects.count() == 1


def test_checkout_waits_for_a_delete_holding_the_customer_lock() -> None:
    customer = shopper()
    _cart(customer, (product(stock=5), 1))
    locked = threading.Event()

    def delete() -> None:
        with transaction.atomic():
            Customer.objects.select_for_update(no_key=True).get(pk=customer.pk)
            locked.set()
            time.sleep(0.5)
            Customer.objects.filter(pk=customer.pk).update(status="deleted", deleted_at=real_now())

    def checkout() -> Any:
        locked.wait(TIMEOUT)
        return services.checkout(customer.pk, _input(100_000))

    results = _race(delete, checkout)
    assert isinstance(results[1], AuthFailed) and results[1].get_codes() == "SESSION_EXPIRED"
    assert not Order.objects.exists()


def test_checkout_and_a_capture_of_another_open_order_do_not_deadlock(settings: Any) -> None:
    settings.CHECKOUT_MAX_OPEN_ORDERS = 2
    seed_policies()
    now = real_now()
    customer = shopper()
    credit(customer, 50_000, now - timedelta(days=1))
    with pinned_clock(now):
        other = place(customer, fulfilment="pickup", when=now)
        payment_id = int(prepare(other)["merchant_prepare_id"])
    _cart(customer, (product(stock=5), 1))
    in_hold = threading.Event()
    capture_done = threading.Event()
    real_hold = loyalty_services.create_hold

    def slow_hold(*args: Any, **kwargs: Any) -> Any:
        in_hold.set()  # the checkout holds the customer-level lock now
        capture_done.wait(3)
        return real_hold(*args, **kwargs)

    def checkout() -> Any:
        return services.checkout(customer.pk, _input(100_000 - 5_000, cashback_to_use=5_000))

    def capture() -> Any:
        in_hold.wait(TIMEOUT)
        try:
            return _post_form(CLICK_COMPLETE, complete_params(other, payment_id))
        finally:
            capture_done.set()

    with mock.patch.object(loyalty_services, "create_hold", slow_hold):
        results = _race(checkout, capture)
    assert isinstance(results[0], int), results
    assert results[1]["error"] == 0, results
    assert Purchase.objects.filter(order_id=other.pk).exists()


def test_pay_switch_vs_click_complete() -> None:
    seed_policies()
    now = real_now()
    order = place(shopper(), fulfilment="pickup", when=now)
    with pinned_clock(now + timedelta(seconds=5)):
        payment_id = int(prepare(order)["merchant_prepare_id"])
    results = _race(
        lambda: services.ensure_payable_payment(order.pk, "payme"),
        lambda: _post_form(CLICK_COMPLETE, complete_params(order, payment_id)),
    )
    switch, answer = results
    assert answer["error"] == 0
    assert isinstance(switch, (AlreadyPaid, PaymentInProgress)), switch
    assert not Payment.objects.filter(order=order, provider="payme", status="paid").exists()
    assert Payment.objects.get(pk=payment_id).status == "paid"


def test_duplicate_concurrent_click_completes() -> None:
    seed_policies()
    now = real_now()
    order = place(shopper(), fulfilment="pickup", when=now)
    with pinned_clock(now + timedelta(seconds=5)):
        payment_id = int(prepare(order)["merchant_prepare_id"])
    params = complete_params(order, payment_id)
    results = _race(lambda: _post_form(CLICK_COMPLETE, params), lambda: _post_form(CLICK_COMPLETE, params))
    assert [r["error"] for r in results] == [0, 0]
    assert Purchase.objects.filter(order_id=order.pk).count() == 1


def test_superseding_prepare_vs_complete_of_the_old_transaction() -> None:
    seed_policies()
    now = real_now()
    order = place(shopper(), fulfilment="pickup", when=now)
    with pinned_clock(now + timedelta(seconds=5)):
        old_id = int(prepare(order)["merchant_prepare_id"])
    results = _race(
        lambda: prepare(order, TXN + 1),
        lambda: _post_form(CLICK_COMPLETE, complete_params(order, old_id)),
    )
    assert results[1]["error"] == 0
    if results[0]["error"] == 0:  # the supersede won: the old capture was refused, complete the new one
        new_id = int(results[0]["merchant_prepare_id"])
        assert Payment.objects.get(pk=old_id).error_code == "late_capture"
        assert _post_form(CLICK_COMPLETE, complete_params(order, new_id, TXN + 1))["error"] == 0
    else:  # the Complete won: the new Prepare found the order paid
        assert results[0]["error"] == -4
    normal = Payment.objects.filter(order=order, status="paid").exclude(error_code="late_capture")
    assert normal.count() == 1


def test_two_payme_creates_with_different_ids() -> None:
    seed_policies()
    now = real_now()
    order = place(shopper(), fulfilment="pickup", provider="payme", when=now)
    stamp = int(now.timestamp() * 1000)
    results = _race(
        lambda: payme(sandbox.create("payme-a", ref(order), tiyin(order), stamp)),
        lambda: payme(sandbox.create("payme-b", ref(order), tiyin(order), stamp)),
    )
    created = [r for r in results if "result" in r]
    refused = [r for r in results if "error" in r]
    assert len(created) == 1 and len(refused) == 1 and refused[0]["error"]["code"] == -31051, results
    winner = Payment.objects.get(order=order, status="pending").provider_txn_id
    assert payme(sandbox.check(winner))["result"]["state"] == 1


def test_expiry_job_vs_a_complete_holding_the_order_lock() -> None:
    seed_policies()
    now = real_now()
    order = place(shopper(), fulfilment="pickup", when=now)
    expires = order.reservation_expires_at
    assert expires is not None
    with pinned_clock(now + timedelta(seconds=5)):
        payment_id = int(prepare(order)["merchant_prepare_id"])
    locked = threading.Event()
    real_mark_paid = services.mark_paid

    def slow_mark_paid(*args: Any, **kwargs: Any) -> Any:
        locked.set()  # the Complete holds the Order lock here
        time.sleep(0.5)
        return real_mark_paid(*args, **kwargs)

    def job() -> Any:
        locked.wait(TIMEOUT)
        return services.expire_due_reservations(expires + timedelta(seconds=30))

    with mock.patch.object(services, "mark_paid", slow_mark_paid):
        results = _race(lambda: _post_form(CLICK_COMPLETE, complete_params(order, payment_id)), job)
    assert results[0]["error"] == 0 and results[1] == []
    order.refresh_from_db()
    assert order.status == "processing"


def test_a_poll_never_applies_a_status_older_than_a_concurrent_webhook() -> None:
    """The poll tracks 200; a webhook arriving meanwhile waits for the parcel lock, re-tracks 300 and applies
    it after the poll, so the shipment ends at the newer status."""
    from glowderma.commerce import tasks
    from glowderma.commerce.tests.factories import OrderFactory
    from glowderma.commerce.tests.test_bts import SECRET
    from glowderma.commerce.tests.webhook_support import BTS_URL
    from glowderma.delivery.models import Shipment, ShipmentEvent
    from glowderma.delivery.tests.factories import ShipmentFactory
    from glowderma.integrations.bts.fake import FakeBts
    from glowderma.integrations.bts.ports import Parcel

    order = OrderFactory.create(paid=True)
    old = real_now() - timedelta(minutes=60)
    shipment = ShipmentFactory.create(
        order=order, created=True, bts_status="100", bts_created_at=old, last_status_at=old
    )
    Order.objects.filter(pk=order.pk).update(status="processing")
    poll_tracked = threading.Event()
    webhook_done = threading.Event()

    def track(self: Any, number: str) -> Parcel:
        if threading.current_thread().name == "poll":
            poll_tracked.set()
            # without the parcel lock the webhook finishes (300) before the poll applies its older 200;
            # with it the webhook waits for the poll, so this wait times out
            webhook_done.wait(2)
            status = "200"
        else:
            status = "300"
        return Parcel(client_id="", tracking_number=number, bts_order_id="", tariff=None, status=status)

    def poll() -> None:
        try:
            tasks.run_bts_poll()
        finally:
            connection.close()

    def webhook() -> None:
        try:
            poll_tracked.wait(TIMEOUT)
            response = APIClient().post(
                BTS_URL, {"clientId": str(order.pk)}, format="json", HTTP_X_GD_WEBHOOK_TOKEN=SECRET
            )
            assert response.status_code == 200, response.content
            webhook_done.set()
        finally:
            connection.close()

    with mock.patch.object(FakeBts, "track", track):
        threads = [threading.Thread(target=poll, name="poll"), threading.Thread(target=webhook)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(TIMEOUT)
    shipment = Shipment.objects.get(pk=shipment.pk)
    assert shipment.bts_status == "300"
    statuses = list(
        ShipmentEvent.objects.filter(shipment=shipment, kind="status_received")
        .order_by("id")
        .values_list("bts_status", flat=True)
    )
    assert statuses[-1] == "300"
    assert Order.objects.get(pk=order.pk).status == "delivered"
