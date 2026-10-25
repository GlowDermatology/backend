"""Concurrency under real transactions: two cancels race (one wins), a cancel races a late capture."""

import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection

from glowderma.commerce import services
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, pay, place, txn
from glowderma.core.api.exceptions import InvalidTransition, NotCancellable
from glowderma.core.seeding import pinned_clock
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
        with pinned_clock(AT + timedelta(minutes=30)):
            return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)

    results = _race(cancel, cancel)
    assert sorted(type(r).__name__ for r in results) == ["CancelResult", "NotCancellable"]
    assert any(isinstance(r, NotCancellable) for r in results)
    assert Refund.objects.count() == 1
    assert OrderEvent.objects.filter(order=order, type="cancelled").count() == 1


def test_cancel_racing_a_late_capture_ends_consistent() -> None:
    staff = admin()
    order = place()

    def cancel() -> Any:
        with pinned_clock(AT + timedelta(minutes=5)):
            return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)

    def capture() -> Any:
        with pinned_clock(AT + timedelta(minutes=5)):
            return services.mark_paid(order.pk, txn(order))

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
