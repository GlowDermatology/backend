"""Concurrency under real transactions: two cancels race (one wins), a cancel races a late capture."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.db import DatabaseError, connection, transaction

from glowderma.catalog.models import ProductStock
from glowderma.commerce import services
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.conftest import _REAL_NOW
from glowderma.commerce.tests.support import (
    AT,
    admin,
    credit,
    pay,
    place,
    product,
    seed_policies,
    ship,
    shopper,
    txn,
)
from glowderma.core import timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition, NotCancellable
from glowderma.core.seeding import pinned_clock
from glowderma.delivery import services as delivery_services
from glowderma.integrations.bts.fake import FakeBts
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import LedgerEntry
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db(transaction=True)
TIMEOUT = 10


def _race(*calls: Callable[[], Any]) -> list[Any]:
    """Run the calls in threads released together; each result is the return value or the exception."""
    start = threading.Event()
    results: list[Any] = [None] * len(calls)

    def worker(index: int, call: Callable[[], Any]) -> None:
        try:
            start.wait(TIMEOUT)
            results[index] = call()
        except Exception as exc:  # noqa: BLE001 - the outcome is asserted by the test
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


def test_two_concurrent_cancels_one_wins() -> None:
    staff = admin()
    order = pay(place(fulfilment="pickup"))

    def cancel() -> Any:
        return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)

    # the clock is pinned once, in the main thread: the workers read the same module attribute (entering
    # pinned_clock in each worker makes the second one restore the first one's lambda for good)
    with pinned_clock(AT + timedelta(minutes=30)):
        results = _race(cancel, cancel)
    assert sorted(type(r).__name__ for r in results) == ["CancelResult", "NotCancellable"]
    assert any(isinstance(r, NotCancellable) for r in results)
    assert Refund.objects.count() == 1
    assert OrderEvent.objects.filter(order=order, type="cancelled").count() == 1


def test_cancel_racing_a_late_capture_ends_consistent() -> None:
    staff = admin()
    order = place()

    def cancel() -> Any:
        return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)

    def capture() -> Any:
        return services.mark_paid(order.pk, txn(order))

    with pinned_clock(AT + timedelta(minutes=5)):
        results = _race(cancel, capture)
    final = Order.objects.get(pk=order.pk)
    payment = Payment.objects.get(order=order)
    assert final.status == "cancelled"
    if isinstance(results[1], InvalidTransition):  # the cancel won: the capture is refused, nothing paid
        assert payment.status == "failed" and not Refund.objects.exists()
        assert not OrderEvent.objects.filter(order=order, type="payment_confirmed").exists()
    else:  # the capture won: the cancel refunds the paid order (the eager dispatcher may submit it already)
        assert results[1] is None and payment.status in ("paid", "refunded")
        assert Refund.objects.get().state in ("requested", "succeeded")
        assert OrderEvent.objects.filter(order=order, type="payment_confirmed").exists()
    assert isinstance(results[0], services.CancelResult)


def test_the_pinned_clock_is_restored_after_a_race() -> None:
    """Regression: the race helpers must leave ``timeutils.now`` as they found it."""
    assert timeutils.now is _REAL_NOW
    assert abs((timeutils.now() - datetime.now(UTC)).total_seconds()) < 5


def test_a_pos_sale_committed_during_the_bts_cancel_does_not_fail_the_cancel() -> None:
    """P1 with real transactions: while the cancel waits for BTS (Order lock held, no account lock yet) a POS
    sale on the same account commits one second later; the reversal is stamped after it."""
    seed_policies()
    staff = admin()
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=1))
    order = ship(place(customer, cashback=10_000))
    instant = AT + timedelta(minutes=30)
    in_bts = threading.Event()
    sale_done = threading.Event()
    real_cancel = delivery_services.cancel

    def slow_bts_cancel(*args: Any, **kwargs: Any) -> delivery_services.CancelOutcome:
        in_bts.set()
        sale_done.wait(TIMEOUT)
        return real_cancel(*args, **kwargs)

    def cancel() -> Any:
        return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)

    def pos_sale() -> Any:
        in_bts.wait(TIMEOUT)
        try:
            return loyalty_services.record_store_purchase(
                customer.pk, "S-RACE-T", "", 500_000, instant, at=instant + timedelta(seconds=1)
            )
        finally:
            sale_done.set()

    with pinned_clock(instant), mock.patch.object(delivery_services, "cancel", side_effect=slow_bts_cancel):
        results = _race(cancel, pos_sale)
    assert isinstance(results[0], services.CancelResult), results
    assert results[0].bts_cancelled and results[0].cashback_reversed
    reversals = LedgerEntry.objects.filter(order_id=order.pk, kind="reversal")
    assert reversals.count() == 2 and {e.at for e in reversals} == {instant + timedelta(seconds=1)}
    assert Order.objects.get(pk=order.pk).status == "cancelled"


def test_admin_paths_wait_for_the_order_lock_at_most_admin_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BTS create holding the order row must not block an admin cancel without limit: 409 ``conflict``
    with the Uzbek message once ``ADMIN_LOCK_WAIT`` runs out, and nothing is written."""
    staff = admin()
    order = pay(place(fulfilment="pickup"))
    monkeypatch.setattr(services, "ADMIN_LOCK_WAIT", timedelta(milliseconds=200))
    locked = threading.Event()
    release = threading.Event()

    def hold_the_row() -> Any:
        with transaction.atomic():
            services.lock_order(order.pk)
            locked.set()
            release.wait(TIMEOUT)

    def cancel() -> Any:
        locked.wait(TIMEOUT)
        try:
            return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
        finally:
            release.set()

    holder, outcome = _race(hold_the_row, cancel)
    assert holder is None
    assert isinstance(outcome, Conflict) and str(outcome.detail) == services.MESSAGE_ORDER_BUSY
    assert Order.objects.get(pk=order.pk).status == "processing" and not Refund.objects.exists()
    # the lock wait is restored after the row is locked: a later statement may wait as usual
    with transaction.atomic():
        services.lock_order(order.pk, wait=timedelta(seconds=1))
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout')")
            assert cursor.fetchone() == ("0",)


def test_two_concurrent_resolves_one_wins_the_other_gets_the_uzbek_message() -> None:
    staff = admin()
    order = pay(place(fulfilment="pickup", provider="payme"))
    with pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    refund = Refund.objects.get()

    def resolve(outcome: str) -> Callable[[], Any]:
        return lambda: services.resolve_refund(refund.pk, outcome, staff.pk)

    results = _race(resolve("succeeded"), resolve("failed"))
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(losers) == 1 and isinstance(losers[0], InvalidTransition)
    assert str(losers[0].detail) == services.MESSAGE_REFUND_NOT_RESOLVABLE
    assert Refund.objects.get().state in ("succeeded", "failed")


def test_checkout_quotes_the_tariff_before_locking_the_stock_rows() -> None:
    """A BTS quote that hangs must not hold the ProductStock row locks of the cart lines."""
    item = product()
    customer = shopper()
    lockable: list[bool] = []
    real_quote = FakeBts.quote

    def probe() -> None:
        try:
            with transaction.atomic():
                ProductStock.objects.select_for_update(nowait=True).filter(product=item).first()
            lockable.append(True)
        except DatabaseError:
            lockable.append(False)
        finally:
            connection.close()

    def quote(self: FakeBts, region_code: str, city_code: str) -> int:
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(TIMEOUT)
        return real_quote(self, region_code, city_code)

    with mock.patch.object(FakeBts, "quote", quote):
        place(customer, [(item, 1)])
    assert lockable == [True]
