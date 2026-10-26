"""``me/cashback`` ``holds[]`` (customer contract §2.9.1): an overdue hold keeps the payment grace only while
its order has an open transaction attached in time, which commerce answers for ``loyalty.hold_grace``."""

from datetime import datetime, timedelta

import pytest
from django.test import override_settings

from glowderma.commerce import selectors
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import (
    AT,
    credit,
    pay,
    pending_payment_id,
    place,
    seed_policies,
    shopper,
)
from glowderma.loyalty import hold_grace
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.payments.models import Payment

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("grace")]
GRACE = timedelta(seconds=300)


@pytest.fixture
def grace() -> object:
    with override_settings(PAYMENT_IN_FLIGHT_GRACE_SECONDS=300) as settings_override:
        yield settings_override


def _order_with_hold() -> Order:
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=1))
    return place(customer, cashback=5_000)


def _attach_click(order: Order, prepared_at: datetime, *, completed: bool = False) -> None:
    click_trans_id = 2875510000 + order.pk
    state: dict[str, object] = {"prepare_at": prepared_at.isoformat(), "click_trans_id": click_trans_id}
    if completed:
        state["complete_at"] = (prepared_at + timedelta(seconds=30)).isoformat()
    Payment.objects.filter(pk=pending_payment_id(order)).update(
        provider_txn_id=str(click_trans_id), provider_state=state
    )


def _deadline(order: Order) -> datetime:
    assert order.reservation_expires_at is not None
    return order.reservation_expires_at


def test_commerce_answers_the_loyalty_hold_grace() -> None:
    assert hold_grace._oracle is selectors.orders_in_payment_grace


def test_only_an_open_transaction_attached_before_the_deadline_keeps_the_grace() -> None:
    seed_policies()
    attached, idle, late, completed = (_order_with_hold() for _ in range(4))
    deadline = _deadline(attached)
    _attach_click(attached, deadline - timedelta(minutes=1))
    _attach_click(late, _deadline(late) + timedelta(seconds=5))
    _attach_click(completed, _deadline(completed) - timedelta(minutes=1), completed=True)
    ids = {attached.pk, idle.pk, late.pk, completed.pk}

    assert selectors.orders_in_payment_grace(ids, deadline - timedelta(seconds=1)) == set()  # not overdue yet
    assert selectors.orders_in_payment_grace(ids, deadline + timedelta(seconds=10)) == {attached.pk}
    assert selectors.orders_in_payment_grace(ids, deadline + GRACE) == set()  # the grace is over

    (shown,) = loyalty_selectors.effective_holds(attached.customer_id, deadline + timedelta(seconds=10))
    assert (shown.order_id, shown.expires_at) == (attached.pk, deadline + GRACE)
    (own,) = loyalty_selectors.effective_holds(idle.customer_id, _deadline(idle) + timedelta(seconds=10))
    assert (own.order_id, own.expires_at) == (idle.pk, _deadline(idle))


def test_a_paid_order_is_never_in_the_grace() -> None:
    seed_policies()
    order = _order_with_hold()
    _attach_click(order, _deadline(order) - timedelta(minutes=1))
    pay(order)
    assert selectors.orders_in_payment_grace({order.pk}, _deadline(order) + timedelta(seconds=10)) == set()
