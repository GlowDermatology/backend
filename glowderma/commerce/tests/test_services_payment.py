"""Payment confirmation (03 §4.1 row 2), provider failures (row 16) and reservation expiry (row 3)."""

from datetime import timedelta
from unittest import mock
from uuid import UUID

import pytest

from glowderma.commerce import services, tasks, topics
from glowderma.commerce.models import OrderEvent, StockReservation
from glowderma.commerce.tests.support import (
    AT,
    credit,
    pay,
    pending_payment_id,
    place,
    product,
    seed_policies,
    shopper,
    txn,
)
from glowderma.core import enums
from glowderma.core.api.exceptions import InvalidTransition
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import Shipment
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import CashbackHold, LedgerEntry, Purchase
from glowderma.payments import services as payment_services
from glowderma.payments.models import Payment

pytestmark = pytest.mark.django_db


def test_mark_paid_runs_every_after_payment_effect() -> None:
    seed_policies()
    customer = shopper()
    credit(customer, 50_000, AT - timedelta(days=1))
    cream, spf = product(312_000), product(240_000, promo=True)
    order = place(customer, [(cream, 1), (spf, 1)], cashback=48_600)
    paid_at = AT + timedelta(minutes=3)
    order = pay(order, paid_at)
    assert (order.status, order.paid_at, order.status_changed_at) == ("processing", paid_at, paid_at)
    payment = Payment.objects.get(order=order)
    assert (payment.status, payment.provider_txn_id, payment.confirmed_at) == ("paid", txn(order), paid_at)
    assert set(StockReservation.objects.filter(order=order).values_list("state", flat=True)) == {"converted"}
    hold = CashbackHold.objects.get(order_id=order.pk)
    assert hold.state == "captured"
    purchase = Purchase.objects.get(order_id=order.pk)
    assert (purchase.amount, purchase.promo_amount, purchase.cashback_paid) == (552_000, 240_000, 48_600)
    entries = list(LedgerEntry.objects.filter(order_id=order.pk).order_by("id"))
    assert [(e.kind, e.amount, e.at) for e in entries] == [
        ("debit", -48_600, paid_at),
        ("credit", 3_120, paid_at),
    ]
    events = list(OrderEvent.objects.filter(order=order).order_by("id"))[2:]
    assert [(e.type, e.status_after, e.at) for e in events] == [
        ("payment_confirmed", "processing", paid_at),
        ("cashback_credited", "", paid_at),
    ]
    assert events[0].data == {
        "provider": "click",
        "transaction_id": txn(order),
        "amount": order.total,
        "reservation_converted": True,
    }
    assert events[1].data == {
        "ledger_entry_id": entries[1].pk,
        "amount": 3_120,
        "base": 312_000,
        "percent": "1.00",
        "policy_version": 3,
    }
    messages = {m.topic: m for m in OutboxMessage.objects.filter(payload__order_id=order.pk)}
    assert set(messages) == {topics.BILLZ_PUSH_SALE, topics.BTS_CREATE_SHIPMENT, topics.PUSH_ORDER_STATUS}
    assert messages[topics.BILLZ_PUSH_SALE].payload == {
        "purchase_id": purchase.pk,
        "order_id": order.pk,
        "billz_order_uuid": str(purchase.billz_order_uuid),
    }
    assert messages[topics.BILLZ_PUSH_SALE].dedupe_key == f"billz.push_sale:{purchase.billz_order_uuid}"
    assert messages[topics.BTS_CREATE_SHIPMENT].payload == {
        "order_id": order.pk,
        "manual": False,
        "request_no": 1,
        "actor_id": None,
    }
    assert messages[topics.BTS_CREATE_SHIPMENT].dedupe_key == f"bts.create_shipment:{order.pk}:1"
    assert Shipment.objects.get(order=order).create_message == messages[topics.BTS_CREATE_SHIPMENT]
    assert messages[topics.PUSH_ORDER_STATUS].payload == {
        "order_id": order.pk,
        "status": "processing",
        "event_id": events[0].pk,
        "customer_id": customer.pk,
    }
    assert messages[topics.PUSH_ORDER_STATUS].dedupe_key == f"push.order_status:{order.pk}:processing"


def test_pickup_payment_creates_no_shipment_and_no_credit_without_policy() -> None:
    order = pay(place(fulfilment=enums.FulfilmentMethod.PICKUP))
    assert not OutboxMessage.objects.filter(topic=topics.BTS_CREATE_SHIPMENT).exists()
    assert not OrderEvent.objects.filter(order=order, type="cashback_credited").exists()
    assert not LedgerEntry.objects.exists()


def test_billz_uuid_pinned_at_checkout_is_used_by_the_purchase() -> None:
    uuid = UUID("0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10")
    order = pay(place(fulfilment="pickup", billz_order_uuid=uuid))
    assert Purchase.objects.get(order_id=order.pk).billz_order_uuid == uuid


def test_mark_paid_is_idempotent_and_refuses_late_captures() -> None:
    order = place()
    payment_id = pending_payment_id(order)
    pay(order)
    with pinned_clock(AT + timedelta(minutes=9)):
        services.mark_paid(order.pk, payment_id, "click", txn(order))  # same transaction: no-op
        with pytest.raises(InvalidTransition):
            services.mark_paid(order.pk, payment_id, "click", "CLK-OTHER")
        with pytest.raises(InvalidTransition):
            services.mark_paid(order.pk, payment_id, "payme", txn(order))  # another provider
    assert OrderEvent.objects.filter(order=order, type="payment_confirmed").count() == 1
    payment = Payment.objects.get(order=order)
    services.confirm_payment_effects(order.pk, payment.pk)  # replay of an applied capture
    assert OrderEvent.objects.filter(order=order, type="payment_confirmed").count() == 1
    with pytest.raises(services.OrderNotFound):
        services.mark_paid(10**9, payment_id, "click", "x")


def test_confirm_effects_guards_the_payment() -> None:
    order = place()
    other = place()
    pending = Payment.objects.get(order=order)
    with pytest.raises(InvalidTransition):
        services.confirm_payment_effects(order.pk, pending.pk)  # still pending
    with pytest.raises(InvalidTransition):
        services.confirm_payment_effects(other.pk, pending.pk)  # another order's payment
    expired = place()
    expired_payment = pending_payment_id(expired)
    with pinned_clock(AT + timedelta(minutes=20)):
        services.expire_reservation(expired.pk)
    with pytest.raises(InvalidTransition):
        services.mark_paid(expired.pk, expired_payment, "click", "CLK-1")
    with pytest.raises(InvalidTransition):  # a payment of another order
        services.mark_paid(order.pk, Payment.objects.get(order=other).pk, "click", "CLK-2")


def test_provider_failure_keeps_the_order_payable() -> None:
    order = place(provider=enums.PaymentProvider.PAYME)
    payment_id = pending_payment_id(order)
    with pinned_clock(AT + timedelta(minutes=2)):
        assert services.fail_payment_effects(order.pk, payment_id, "-31008")
        assert not services.fail_payment_effects(order.pk, payment_id, "-31008")  # failed already
        with pytest.raises(InvalidTransition):
            services.fail_payment_effects(place().pk, payment_id, "-31008")  # another order's payment
    order.refresh_from_db()
    assert order.status == "awaiting_payment"
    event = OrderEvent.objects.get(order=order, type="payment_failed")
    assert event.data == {"provider": "payme", "transaction_id": None, "error_code": "-31008"}


def test_expire_reservation_releases_everything_silently() -> None:
    seed_policies()
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=1))
    item = product(stock=1)
    order = place(customer, [(item, 1)], cashback=10_000)
    with pinned_clock(AT + timedelta(minutes=14)):
        assert not services.expire_reservation(order.pk)  # not due yet
    due = AT + timedelta(minutes=15)
    with pinned_clock(due):
        assert services.expire_reservation(order.pk)
        assert not services.expire_reservation(order.pk)  # already cancelled
    order.refresh_from_db()
    assert (order.status, order.cancellation_reason, order.cancelled_at) == (
        "cancelled",
        "reservation_expired",
        due,
    )
    reservation = StockReservation.objects.get(order=order)
    assert (reservation.state, reservation.release_reason) == ("released", "expired")
    hold = CashbackHold.objects.get(order_id=order.pk)
    assert (hold.state, hold.release_reason) == ("released", "reservation_expired")
    payment = Payment.objects.get(order=order)
    assert (payment.status, payment.error_code) == ("failed", "expired")
    event = OrderEvent.objects.get(order=order, type="reservation_expired")
    assert (event.status_after, event.data) == (
        "cancelled",
        {"released_item_count": 1, "payment_failed": True},
    )
    assert not OrderEvent.objects.filter(order=order, type="payment_failed").exists()
    assert not OutboxMessage.objects.filter(topic=topics.PUSH_ORDER_STATUS).exists()  # no push (N-1)
    place(lines=[(item, 1)])  # the unit is available again


def test_payme_state_one_transaction_is_not_failed_unilaterally() -> None:
    order = place(provider=enums.PaymentProvider.PAYME)
    Payment.objects.filter(order=order).update(provider_state={"state": 1})
    with pinned_clock(AT + timedelta(minutes=16)):
        assert services.expire_reservation(order.pk)
    assert Payment.objects.get(order=order).status == "pending"
    assert OrderEvent.objects.get(order=order, type="reservation_expired").data["payment_failed"] is False


def test_beat_task_expires_due_orders_only() -> None:
    due = place(when=AT)
    fresh = place(when=AT + timedelta(minutes=10))
    paid = pay(place(when=AT))
    with pinned_clock(AT + timedelta(minutes=16)):
        assert tasks.expire_reservations() == [due.pk]
    for order, status in ((due, "cancelled"), (fresh, "awaiting_payment"), (paid, "processing")):
        order.refresh_from_db()
        assert order.status == status
    with pinned_clock(AT + timedelta(minutes=30)):
        assert services.expire_due_reservations() == [fresh.pk]


def test_an_entry_posted_before_the_capture_locks_the_account_does_not_fail_mark_paid() -> None:
    """P1b: a POS sale on the same account lands between the capture's clock read and its account lock; the
    debit, the credit and the order effects are stamped at the newest entry, the capture never fails."""
    seed_policies()
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=1))
    order = place(customer, cashback=10_000)
    instant = AT + timedelta(minutes=3)
    real_confirm = payment_services.confirm_payment

    def confirm_with_a_pos_sale(*args: object, **kwargs: object) -> payment_services.PaymentResult:
        result = real_confirm(*args, **kwargs)  # type: ignore[arg-type]
        loyalty_services.record_store_purchase(
            customer.pk, "S-RACE-2", "", 500_000, instant, at=instant + timedelta(seconds=2)
        )
        return result

    with (
        pinned_clock(instant),
        mock.patch.object(payment_services, "confirm_payment", confirm_with_a_pos_sale),
    ):
        services.mark_paid(order.pk, pending_payment_id(order), "click", txn(order))
    order.refresh_from_db()
    assert order.status == "processing" and order.paid_at == instant + timedelta(seconds=2)
    debit = LedgerEntry.objects.get(order_id=order.pk, kind="debit")
    assert debit.at == instant + timedelta(seconds=2)
    assert Payment.objects.get(order=order).status == "paid"
