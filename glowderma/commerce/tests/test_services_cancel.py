"""commerce.services.cancel_order — contract §2.2 cancel table (03 §4.1 rows 4/6/7; 02 §1.10)."""

from datetime import timedelta
from unittest import mock

import pytest

from glowderma.commerce import services, topics
from glowderma.commerce.models import Order, OrderEvent, StockReservation
from glowderma.commerce.tests.support import AT, admin, credit, pay, place, seed_policies, ship, shopper
from glowderma.core import enums
from glowderma.core.api.exceptions import NotCancellable, ProviderUnavailable
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.bts.fake import FakeBts
from glowderma.integrations.bts.ports import Parcel
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import CashbackHold, LedgerEntry, Purchase
from glowderma.payments import services as payment_services
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db
NOTE = "Mijoz so‘rovi bilan"


def _cancel(order: Order, actor_id: int, minutes: int = 60) -> services.CancelResult:
    with pinned_clock(order.created_at + timedelta(minutes=minutes)):
        return services.cancel_order(order.pk, f"  {NOTE} ", actor_id)


def test_cancel_awaiting_payment_releases_and_fails_the_payment() -> None:
    seed_policies()
    staff = admin()
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=1))
    order = place(customer, cashback=10_000)
    result = _cancel(order, staff.pk, minutes=5)
    assert result == services.CancelResult(order.pk, None, False, True, False)
    order.refresh_from_db()
    assert (order.status, order.cancellation_reason, order.cancellation_note, order.cancelled_by_id) == (
        "cancelled",
        "admin",
        NOTE,
        staff.pk,
    )
    assert StockReservation.objects.get(order=order).release_reason == "cancelled"
    assert CashbackHold.objects.get(order_id=order.pk).release_reason == "order_cancelled"
    assert Payment.objects.get(order=order).status == "failed"
    assert not Refund.objects.exists() and not OrderEvent.objects.filter(type="payment_failed").exists()
    event = OrderEvent.objects.get(order=order, type="cancelled")
    assert (event.source, event.actor_id, event.status_after) == ("admin", staff.pk, "cancelled")
    assert event.data == {
        "reason": "admin",
        "note": NOTE,
        "refund_id": None,
        "cashback_reversed": False,
        "stock_released": True,
        "bts_cancelled": False,
        "push_sent": True,
    }
    push = OutboxMessage.objects.get(topic=topics.PUSH_ORDER_STATUS)
    assert push.payload["status"] == "cancelled" and push.payload["event_id"] == event.pk
    audit = AuditEntry.objects.get(action="order.cancelled")
    assert (audit.entity_type, audit.entity_id, audit.actor_id) == ("order", str(order.pk), staff.pk)
    assert audit.after == {"reason": "admin", "note": NOTE, "refund_id": None, "bts_cancelled": False}


def test_cancel_processing_click_requests_a_refund_and_reverses_the_entries() -> None:
    seed_policies()
    staff = admin()
    customer = shopper(device=False)
    credit(customer, 30_000, AT - timedelta(days=1))
    order = pay(place(customer, cashback=10_000))  # no parcel yet (BTS message pending)
    result = _cancel(order, staff.pk)
    refund = Refund.objects.get()
    assert result == services.CancelResult(order.pk, refund.pk, True, False, False)
    assert (refund.state, refund.amount, refund.requested_by_id) == ("requested", order.total, staff.pk)
    message = OutboxMessage.objects.get(topic=topics.REFUND_SUBMIT)
    assert message.dedupe_key == f"refund.submit:{refund.pk}"
    assert message.payload == {
        "refund_id": refund.pk,
        "payment_id": refund.payment_id,
        "order_id": order.pk,
        "provider_txn_id": Payment.objects.get(order=order).provider_txn_id,
        "amount": order.total,
    }
    assert Payment.objects.get(order=order).status == "paid"  # until the provider confirms (O5)
    reversals = list(LedgerEntry.objects.filter(order_id=order.pk, kind="reversal").order_by("id"))
    debit = LedgerEntry.objects.get(order_id=order.pk, kind="debit")
    credit_entry = LedgerEntry.objects.get(order_id=order.pk, kind="credit")
    assert [(r.reverses_id, r.amount) for r in reversals] == [
        (debit.pk, 10_000),
        (credit_entry.pk, -credit_entry.amount),
    ]
    assert Purchase.objects.get(order_id=order.pk).reversed
    assert (
        StockReservation.objects.get(order=order).state == "converted"
    )  # a paid sale is a Billz return (O2)
    events = {e.type: e for e in OrderEvent.objects.filter(order=order)}
    assert (
        events["cancelled"].data["refund_id"] == refund.pk and events["cancelled"].data["push_sent"] is False
    )
    assert events["cancelled"].data["cashback_reversed"] is True
    assert events["refund_requested"].data == {
        "refund_id": refund.pk,
        "amount": order.total,
        "provider": "click",
        "manual": False,
        "manual_reason": None,
    }
    assert events["refund_requested"].source == "admin"
    assert AuditEntry.objects.filter(action="refund.requested", entity_id=str(refund.pk)).exists()


def test_cancel_processing_payme_needs_a_manual_refund() -> None:
    staff = admin()
    order = pay(place(provider=enums.PaymentProvider.PAYME, fulfilment="pickup"))
    _cancel(order, staff.pk)
    refund = Refund.objects.get()
    assert refund.state == "manual_required"
    assert not OutboxMessage.objects.filter(topic=topics.REFUND_SUBMIT).exists()
    assert OrderEvent.objects.get(order=order, type="refund_requested").data["manual"] is True


def test_cancel_with_a_parcel_cancels_at_bts_first() -> None:
    staff = admin()
    order = ship(place())
    result = _cancel(order, staff.pk)
    assert result.bts_cancelled
    shipment = Shipment.objects.get(order=order)
    assert shipment.state == "cancelled" and shipment.cancelled_at is not None
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="cancelled").exists()
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is True


def test_handed_to_courier_is_cancellable_only_while_bts_reports_100() -> None:
    staff = admin()
    order = ship(place())
    with pinned_clock(AT + timedelta(minutes=30)):
        services.transition(order.pk, "handed_to_courier", staff.pk)
    result = _cancel(order, staff.pk)
    assert result.bts_cancelled and Order.objects.get(pk=order.pk).status == "cancelled"


def _snapshot() -> dict[str, object]:
    return {
        "orders": list(Order.objects.values_list("id", "status", "updated_at")),
        "events": OrderEvent.objects.count(),
        "refunds": Refund.objects.count(),
        "ledger": LedgerEntry.objects.count(),
        "shipments": list(Shipment.objects.values_list("id", "state", "updated_at")),
        "shipment_events": ShipmentEvent.objects.count(),
        "outbox": OutboxMessage.objects.count(),
        "audit": AuditEntry.objects.count(),
        "payments": list(Payment.objects.values_list("id", "status")),
    }


def test_bts_refusal_is_409_with_the_bts_message_and_writes_nothing() -> None:
    staff = admin()
    order = ship(place())
    with pinned_clock(AT + timedelta(hours=2)):
        services.apply_bts_status(order.pk, "200", "webhook")  # handed to the courier at BTS
    before = _snapshot()
    with pytest.raises(NotCancellable) as caught:
        _cancel(order, staff.pk, minutes=180)
    assert str(caught.value.detail) == "BTS buyurtmani bekor qilmadi: posilka kuryerda."
    assert _snapshot() == before


def test_bts_transport_failure_is_503_and_writes_nothing() -> None:
    staff = admin()
    order = ship(place(fake={"bts": {"cancel": "unavailable"}}))
    before = _snapshot()
    with pytest.raises(ProviderUnavailable):
        _cancel(order, staff.pk)
    assert _snapshot() == before


def test_forced_refusal_from_the_fake_scenario() -> None:
    order = ship(place(fake={"bts": {"cancel": "refused"}}))
    with pytest.raises(NotCancellable):
        _cancel(order, admin().pk)


@pytest.mark.parametrize("status", ["delivered", "cancelled"])
def test_terminal_orders_are_not_cancellable(status: str) -> None:
    staff = admin()
    order = pay(place(fulfilment="pickup"))
    with pinned_clock(AT + timedelta(hours=1)):
        if status == "delivered":
            services.transition(order.pk, "delivered", staff.pk)
        else:
            services.cancel_order(order.pk, NOTE, staff.pk)
    with pytest.raises(NotCancellable) as caught:
        _cancel(order, staff.pk, minutes=120)
    assert str(caught.value.detail) == "Bu buyurtmani bekor qilib bo‘lmaydi."


def test_reason_is_checked_by_the_service_too() -> None:
    with pytest.raises(ValueError):
        services.cancel_order(place().pk, "  x ", admin().pk)
    with pytest.raises(services.OrderNotFound):
        services.cancel_order(10**9, NOTE, admin().pk)


# --- review fixes: the BTS cancel is repeatable, the reversal instant is read under the account lock -------


def test_a_failure_after_the_bts_cancel_rolls_back_and_the_retried_cancel_succeeds() -> None:
    """BTS agreed, then a later step raised: the transaction rolled back (order processing, shipment
    created/100) while BTS holds a cancelled parcel. The retried cancel asks BTS again, which answers ok
    for a parcel it already cancelled, and the cancel completes."""
    staff = admin()
    order = ship(place())
    with mock.patch.object(payment_services, "create_refund", side_effect=RuntimeError("database lost")):
        with pytest.raises(RuntimeError):
            _cancel(order, staff.pk)
    order.refresh_from_db()
    shipment = Shipment.objects.get(order=order)
    assert (order.status, shipment.state, shipment.bts_status) == ("processing", "created", "100")
    assert str(order.pk) in fake_bts._cancelled  # BTS did cancel the parcel
    assert not Refund.objects.exists() and not OrderEvent.objects.filter(type="cancelled").exists()
    result = _cancel(order, staff.pk, minutes=61)
    assert result.bts_cancelled and result.refund_id is not None
    assert Shipment.objects.get(order=order).state == "cancelled"
    assert Order.objects.get(pk=order.pk).status == "cancelled"


def test_fake_bts_cancel_is_idempotent_at_the_provider_boundary() -> None:
    parcel = Parcel(
        client_id="777", tracking_number="48200777", bts_order_id="BTS-777", tariff=25_000, status="100"
    )
    assert FakeBts().cancel(parcel).ok
    # already cancelled: ok even where a first cancel would be refused
    assert FakeBts({"cancel": "refused"}).cancel(parcel).ok
    assert FakeBts({"status": "200"}).cancel(parcel).ok


def test_an_entry_posted_during_the_cancel_window_does_not_fail_the_reversal() -> None:
    """P1: the cancel read its instant, then (during the BTS call) a POS sale was posted on the same account
    one second later. The reversal is stamped under the account lock (≥ that entry): no OutOfOrderPosting."""
    seed_policies()
    staff = admin()
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=1))
    order = ship(place(customer, cashback=10_000))
    instant = order.created_at + timedelta(minutes=60)
    real_cancel = delivery_services.cancel

    def bts_cancel_with_a_pos_sale(*args: object, **kwargs: object) -> delivery_services.CancelOutcome:
        loyalty_services.record_store_purchase(
            customer.pk, "S-RACE-1", "", 500_000, instant, at=instant + timedelta(seconds=1)
        )
        return real_cancel(*args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(delivery_services, "cancel", side_effect=bts_cancel_with_a_pos_sale):
        result = _cancel(order, staff.pk)
    assert result.bts_cancelled and result.cashback_reversed
    reversals = LedgerEntry.objects.filter(order_id=order.pk, kind="reversal")
    assert reversals.count() == 2
    assert all(entry.at == instant + timedelta(seconds=1) for entry in reversals)
    order.refresh_from_db()
    assert (order.status, order.cancelled_at) == ("cancelled", instant + timedelta(seconds=1))
