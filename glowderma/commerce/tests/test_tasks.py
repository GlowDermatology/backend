"""commerce.tasks — outbox handlers at the provider boundary and the dead-letter hooks (design 03 §5)."""

from datetime import timedelta
from typing import Any
from unittest import mock

import pytest

from glowderma.commerce import services, tasks, topics
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place
from glowderma.core import outbox
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.core.tasks import dispatch_outbox
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.bts.ports import BtsUnavailable
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.click.ports import ClickRefundRejected
from glowderma.loyalty.models import Purchase
from glowderma.payments import services as payment_services
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db


def _message(topic: str, order_id: int) -> OutboxMessage:
    return OutboxMessage.objects.get(topic=topic, payload__order_id=order_id)


def _dispatch_until_done(message: OutboxMessage, rounds: int = 12) -> OutboxMessage:
    """Run the real dispatcher (attempts, backoff, dead letters) until the message is final."""
    for _ in range(rounds):
        OutboxMessage.objects.filter(pk=message.pk).update(available_at=AT - timedelta(days=1))
        OutboxMessage.objects.exclude(pk=message.pk).update(status="done")
        dispatch_outbox()
        message.refresh_from_db()
        if message.status in ("done", "dead"):
            break
    return message


# --- bts.create_shipment ------------------------------------------------------------------------------------


def test_bts_create_success() -> None:
    order = pay(place())
    with pinned_clock(AT + timedelta(minutes=4)):
        deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    shipment = Shipment.objects.get(order=order)
    expected = str(48_200_000 + order.pk % 100_000)
    assert (shipment.state, shipment.tracking_number, shipment.tariff, shipment.bts_status, shipment.via) == (
        "created",
        expected,
        25_000,
        "100",
        "polling",
    )
    assert (shipment.attempts, shipment.bts_created_at) == (1, AT + timedelta(minutes=4))
    event = OrderEvent.objects.get(order=order, type="shipment_created")
    assert (event.source, event.actor_id, event.data) == (
        "system",
        None,
        {"tracking_number": expected, "tariff": 25_000, "manual": False},
    )


def test_bts_create_definitive_failure_is_recorded_not_raised() -> None:
    order = pay(place(fake={"bts": {"fail_attempts": 2}}))
    deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    event = OrderEvent.objects.get(order=order, type="shipment_failed")
    assert event.data == {"attempt": 1, "manual": False, "error_code": "address_not_found"}
    assert Shipment.objects.get(order=order).last_attempt_at is not None


def test_provider_succeeded_then_worker_crashed_adopts_the_parcel() -> None:
    """BTS created the parcel but the worker died before the commit: the redelivery finds it by clientId."""
    order = pay(place())
    message = _message(topics.BTS_CREATE_SHIPMENT, order.pk)
    with mock.patch.object(delivery_services, "mark_created", side_effect=RuntimeError("worker lost")):
        with pytest.raises(RuntimeError):
            tasks.handle_bts_create_shipment(message)
    assert Shipment.objects.get(order=order).state == "not_created"  # rolled back
    assert str(order.pk) in fake_bts._parcels  # but BTS has it
    with mock.patch.object(fake_bts.FakeBts, "create", side_effect=AssertionError("must not create twice")):
        tasks.handle_bts_create_shipment(message)
        tasks.handle_bts_create_shipment(message)  # a further redelivery: shipment created → no-op
    assert Shipment.objects.filter(order=order, state="created").count() == 1
    assert OrderEvent.objects.filter(order=order, type="shipment_created").count() == 1


def test_existing_parcel_switch_is_adopted() -> None:
    order = pay(place(fake={"bts": {"existing": True, "tracking_number": "48299999"}}))
    deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    assert Shipment.objects.get(order=order).tracking_number == "48299999"


def test_bts_create_skips_orders_that_are_no_longer_processing() -> None:
    staff = admin()
    order = pay(place())
    with pinned_clock(AT + timedelta(minutes=30)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    assert Shipment.objects.get(order=order).state == "not_created"
    assert not OrderEvent.objects.filter(order=order, type__startswith="shipment").exists()


def test_bts_transport_errors_retry_then_dead_letter_marks_the_shipment_failed() -> None:
    order = pay(place(fake={"bts": {"down": True}}))
    message = _dispatch_until_done(_message(topics.BTS_CREATE_SHIPMENT, order.pk))
    assert (message.status, message.attempts) == ("dead", 10)
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.last_error_code, shipment.attempts) == ("failed", "dead_letter", 1)
    event = OrderEvent.objects.get(order=order, type="shipment_failed")
    assert event.data == {"attempt": 1, "manual": False, "error_code": "dead_letter"}
    assert AuditEntry.objects.filter(action="outbox.dead").count() == 1
    # the hook is idempotent and ignores orders that moved on
    tasks.on_bts_create_dead(message)
    assert OrderEvent.objects.filter(order=order, type="shipment_failed").count() == 1


def test_dead_letter_hook_for_a_manual_retry_names_the_admin() -> None:
    staff = admin()
    order = pay(place(fake={"bts": {"fail_attempts": 1}}))
    deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    services.retry_shipment(order.pk, staff.pk)
    message = Shipment.objects.get(order=order).create_message
    assert message is not None
    message.last_error = "timeout"
    tasks.on_bts_create_dead(message)
    event = OrderEvent.objects.filter(order=order, type="shipment_failed").order_by("-id").first()
    assert event is not None and (event.source, event.actor_id, event.data["manual"]) == (
        "admin",
        staff.pk,
        True,
    )
    assert event.data["attempt"] == 2


def test_a_second_dead_letter_after_a_manual_retry_is_recorded() -> None:
    """The automatic create dies, the admin retries, the retry dies too: both are ``shipment_failed``."""
    staff = admin()
    order = pay(place(fake={"bts": {"down": True}}))
    first = _dispatch_until_done(_message(topics.BTS_CREATE_SHIPMENT, order.pk))
    assert first.status == "dead"
    services.retry_shipment(order.pk, staff.pk)
    retry = Shipment.objects.get(order=order).create_message
    assert retry is not None and retry.pk != first.pk
    assert _dispatch_until_done(retry).status == "dead"
    events = OrderEvent.objects.filter(order=order, type="shipment_failed").order_by("id")
    assert [e.data for e in events] == [
        {"attempt": 1, "manual": False, "error_code": "dead_letter"},
        {"attempt": 2, "manual": True, "error_code": "dead_letter"},
    ]
    assert events[1].actor_id == staff.pk
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.attempts, shipment.last_error_code) == ("failed", 2, "dead_letter")
    tasks.on_bts_create_dead(retry)  # a second notice of the same message: nothing new
    assert Shipment.objects.get(order=order).attempts == 2
    assert OrderEvent.objects.filter(order=order, type="shipment_failed").count() == 2


def test_redelivered_create_after_a_definitive_refusal_does_not_ask_bts_again() -> None:
    """``mark_failed`` committed, then the worker died before the message was done: the redelivery skips."""
    staff = admin()
    order = pay(place(fake={"bts": {"fail_attempts": 1}}))
    message = _message(topics.BTS_CREATE_SHIPMENT, order.pk)
    tasks.handle_bts_create_shipment(message)
    with mock.patch.object(fake_bts.FakeBts, "create", side_effect=AssertionError("asked twice")):
        tasks.handle_bts_create_shipment(message)
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.attempts) == ("failed", 1)  # no parcel created without an admin retry
    assert OrderEvent.objects.filter(order=order, type="shipment_failed").count() == 1
    OutboxMessage.objects.filter(pk=message.pk).update(status="done")  # the redelivery's completion
    services.retry_shipment(order.pk, staff.pk)
    deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    assert Shipment.objects.get(order=order).state == "created"
    tasks.handle_bts_create_shipment(message)  # the superseded request 1 once more: still nothing
    assert OrderEvent.objects.filter(order=order, type__startswith="shipment").count() == 2


def test_crash_then_cancel_cancels_the_orphan_parcel_at_bts() -> None:
    """P3: BTS created the parcel, the worker crashed before commit, the admin cancelled (no parcel seen):
    the redelivered create finds the parcel by clientId and cancels it at BTS."""
    staff = admin()
    order = pay(place())
    message = _message(topics.BTS_CREATE_SHIPMENT, order.pk)
    with mock.patch.object(delivery_services, "mark_created", side_effect=RuntimeError("worker lost")):
        with pytest.raises(RuntimeError):
            tasks.handle_bts_create_shipment(message)
    with pinned_clock(AT + timedelta(minutes=30)):
        result = services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    assert not result.bts_cancelled and str(order.pk) in fake_bts._parcels
    with pinned_clock(AT + timedelta(minutes=31)):
        tasks.handle_bts_create_shipment(message)
    assert str(order.pk) not in fake_bts._parcels and str(order.pk) in fake_bts._cancelled
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.tracking_number, shipment.cancelled_at) == (
        "cancelled",
        fake_bts.tracking_number_for(order.pk),
        AT + timedelta(minutes=31),
    )
    assert list(shipment.events.values_list("kind", flat=True)) == ["created", "cancelled"]
    assert not OrderEvent.objects.filter(order=order, type__startswith="shipment").exists()
    tasks.handle_bts_create_shipment(message)  # idempotent
    assert shipment.events.count() == 2


def test_an_orphan_parcel_bts_refuses_to_cancel_is_recorded_and_alerted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    staff = admin()
    order = pay(place())
    message = _message(topics.BTS_CREATE_SHIPMENT, order.pk)
    with mock.patch.object(delivery_services, "mark_created", side_effect=RuntimeError("worker lost")):
        with pytest.raises(RuntimeError):
            tasks.handle_bts_create_shipment(message)
    services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    Order.objects.filter(pk=order.pk).update(metadata={"fake": {"bts": {"cancel": "refused"}}})
    tasks.handle_bts_create_shipment(message)
    shipment = Shipment.objects.get(order=order)
    assert shipment.state == "created" and shipment.tracking_number
    assert list(shipment.events.values_list("kind", flat=True)) == ["created", "cancel_refused"]
    assert "refused to cancel the orphan parcel" in caplog.text


def test_orphan_lookup_transport_error_raises_for_a_retry() -> None:
    staff = admin()
    order = pay(place())
    services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    Order.objects.filter(pk=order.pk).update(metadata={"fake": {"bts": {"down": True}}})
    with pytest.raises(BtsUnavailable):
        tasks.handle_bts_create_shipment(_message(topics.BTS_CREATE_SHIPMENT, order.pk))


# --- billz.push_sale ----------------------------------------------------------------------------------------


def test_billz_push_links_the_purchase_and_records_the_event_once() -> None:
    order = pay(place(fulfilment="pickup"))
    message = _message(topics.BILLZ_PUSH_SALE, order.pk)
    with pinned_clock(AT + timedelta(minutes=3, seconds=20)):
        tasks.handle_billz_push_sale(message)
        tasks.handle_billz_push_sale(message)  # redelivery: billz_pushed_at set → skip
    purchase = Purchase.objects.get(order_id=order.pk)
    assert purchase.billz_pushed_at == AT + timedelta(minutes=3, seconds=20)
    event = OrderEvent.objects.get(order=order, type="billz_sale_recorded")
    assert event.data == {"billz_order_id": str(purchase.billz_order_uuid)}


def test_billz_push_transport_error_and_conflict_raise() -> None:
    order = pay(place(fulfilment="pickup", fake={"billz": {"push": "down"}}))
    message = _message(topics.BILLZ_PUSH_SALE, order.pk)
    with pytest.raises(Exception, match="fake: down"):
        tasks.handle_billz_push_sale(message)
    other = pay(place(fulfilment="pickup"))
    wrong: dict[str, Any] = {**_message(topics.BILLZ_PUSH_SALE, other.pk).payload}
    wrong["billz_order_uuid"] = "00000000-0000-4000-8000-000000000000"
    with pytest.raises(Exception, match="pushed as Billz order"):
        tasks.push_sale(wrong)
    with pytest.raises(LookupError):
        tasks.push_sale({**wrong, "purchase_id": 10**9})
    assert not OrderEvent.objects.filter(type="billz_sale_recorded").exists()


# --- refund.submit ------------------------------------------------------------------------------------------


def _cancelled_click_order(**fake: Any) -> tuple[Any, Refund]:
    staff = admin()
    order = pay(place(fulfilment="pickup", fake={"click": fake} if fake else None))
    with pinned_clock(AT + timedelta(minutes=60)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    return order, Refund.objects.get(payment__order=order)


def test_refund_succeeds_and_turns_the_payment_refunded() -> None:
    order, refund = _cancelled_click_order()
    with pinned_clock(AT + timedelta(minutes=62)):
        deliver(topics.REFUND_SUBMIT, order.pk)
    refund.refresh_from_db()
    assert (refund.state, refund.attempts, refund.completed_at) == (
        "succeeded",
        1,
        AT + timedelta(minutes=62),
    )
    assert refund.provider_refund_id.startswith("CLKR-")
    payment = Payment.objects.get(order=order)
    assert (payment.status, payment.refunded_at) == ("refunded", AT + timedelta(minutes=62))
    event = OrderEvent.objects.get(order=order, type="refund_succeeded")
    assert event.data == {"refund_id": refund.pk, "amount": refund.amount, "provider": "click"}
    transitions = list(
        AuditEntry.objects.filter(action="refund.state_changed")
        .order_by("id")
        .values_list("after", flat=True)
    )
    assert [(a["from"], a["to"]) for a in transitions] == [  # type: ignore[index]
        ("requested", "submitting"),
        ("submitting", "succeeded"),
    ]


def test_refund_rejected_is_failed() -> None:
    order, refund = _cancelled_click_order(refund="failed")
    deliver(topics.REFUND_SUBMIT, order.pk)
    refund.refresh_from_db()
    assert (refund.state, refund.error_code) == ("failed", "refund_rejected")
    event = OrderEvent.objects.get(order=order, type="refund_failed")
    assert event.data == {
        "refund_id": refund.pk,
        "amount": refund.amount,
        "provider": "click",
        "state": "failed",
        "error_code": "refund_rejected",
    }
    assert Payment.objects.get(order=order).status == "paid"


def test_refund_unknown_is_never_retried() -> None:
    order, refund = _cancelled_click_order(refund="unknown")
    message = _message(topics.REFUND_SUBMIT, order.pk)
    tasks.handle_refund_submit(message)  # returns normally: the dispatcher marks the message done
    refund.refresh_from_db()
    assert refund.state == "unknown" and refund.submitted_at is not None
    assert OrderEvent.objects.get(order=order, type="refund_failed").data["state"] == "unknown"
    with mock.patch(
        "glowderma.integrations.click.fake.FakeClick.refund", side_effect=AssertionError("retried")
    ):
        tasks.handle_refund_submit(message)  # a redelivery does nothing
    assert Refund.objects.get(pk=refund.pk).attempts == 1


def test_refund_crash_after_the_reversal_is_adopted_by_the_status_check() -> None:
    order, refund = _cancelled_click_order()
    message = _message(topics.REFUND_SUBMIT, order.pk)
    with mock.patch.object(payment_services, "mark_succeeded", side_effect=RuntimeError("worker lost")):
        with pytest.raises(RuntimeError):
            tasks.handle_refund_submit(message)
    refund.refresh_from_db()
    assert refund.state == "submitting"  # committed before the provider call
    with mock.patch(
        "glowderma.integrations.click.fake.FakeClick.refund", side_effect=AssertionError("twice")
    ):
        tasks.handle_refund_submit(message)
    refund.refresh_from_db()
    assert refund.state == "succeeded" and refund.attempts == 1
    assert OrderEvent.objects.filter(order=order, type="refund_succeeded").count() == 1


def test_refund_redelivery_resubmits_when_click_did_not_reverse() -> None:
    """The previous claim is older than the outbox lease: its worker cannot still be talking to Click."""
    order, refund = _cancelled_click_order()
    with pinned_clock(AT + timedelta(minutes=61)):
        payment_services.mark_submitting(refund.pk)
    with pinned_clock(AT + timedelta(minutes=61) + tasks.RESUBMIT_AFTER):
        tasks.handle_refund_submit(_message(topics.REFUND_SUBMIT, order.pk))
    refund.refresh_from_db()
    assert (refund.state, refund.attempts) == ("succeeded", 2)


def test_refund_redelivery_never_doubles_a_fresh_claim() -> None:
    """P2: a second worker re-claims the message while the first one is still waiting for Click — it must
    neither submit a second reversal nor touch the refund; the first worker resolves it."""
    order, refund = _cancelled_click_order()
    with pinned_clock(AT + timedelta(minutes=61)):
        payment_services.mark_submitting(refund.pk)  # worker 1 claimed and is calling Click
    with (
        pinned_clock(AT + timedelta(minutes=63)),
        mock.patch(
            "glowderma.integrations.click.fake.FakeClick.refund", side_effect=AssertionError("doubled")
        ),
    ):
        tasks.handle_refund_submit(_message(topics.REFUND_SUBMIT, order.pk))
    refund.refresh_from_db()
    assert (refund.state, refund.attempts) == ("submitting", 1)
    assert not OrderEvent.objects.filter(order=order, type__in=["refund_failed", "refund_succeeded"]).exists()


def test_refund_claim_is_a_compare_and_set() -> None:
    order, refund = _cancelled_click_order()
    with pinned_clock(AT + timedelta(minutes=61)):
        first = payment_services.mark_submitting(refund.pk)
        with pytest.raises(payment_services.StaleRefundClaim):  # requested → already claimed
            payment_services.mark_submitting(refund.pk)
    later = AT + timedelta(minutes=61) + tasks.RESUBMIT_AFTER
    with pinned_clock(later):
        with pytest.raises(payment_services.StaleRefundClaim):  # wrong attempt number
            payment_services.mark_submitting(
                refund.pk,
                expected_state="submitting",
                expected_attempts=5,
                resubmit_after=tasks.RESUBMIT_AFTER,
            )
        second = payment_services.mark_submitting(
            refund.pk,
            expected_state="submitting",
            expected_attempts=first.attempts,
            resubmit_after=tasks.RESUBMIT_AFTER,
        )
    assert second.attempts == 2
    # the stale worker 1 now gets a refusal: it must not fail the refund attempt 2 owns
    stale = payment_services.mark_failed(refund.pk, "refund_rejected", expected_attempts=first.attempts)
    assert not stale.changed and Refund.objects.get(pk=refund.pk).state == "submitting"
    assert not payment_services.mark_unknown(refund.pk, "timeout", expected_attempts=first.attempts).changed
    with pytest.raises(ValueError):
        payment_services.mark_submitting(refund.pk, expected_state="unknown")


def test_refund_refusal_of_a_duplicate_is_checked_against_the_status() -> None:
    """Click refuses a reversal it already performed: the follow-up status check makes it ``succeeded``."""
    order, refund = _cancelled_click_order()
    txn = refund.payment.provider_txn_id

    def duplicate(provider_txn_id: str, amount: int) -> Any:
        fake_click._reversed.add(txn)  # the reversal of the other worker went through meanwhile
        raise ClickRefundRejected("duplicate", "already reversed")

    with mock.patch("glowderma.integrations.click.fake.FakeClick.refund", side_effect=duplicate):
        tasks.handle_refund_submit(_message(topics.REFUND_SUBMIT, order.pk))
    refund.refresh_from_db()
    assert refund.state == "succeeded" and Payment.objects.get(order=order).status == "refunded"
    assert not OrderEvent.objects.filter(order=order, type="refund_failed").exists()


def test_refund_dead_letter_hook_leaves_a_claimed_refund_to_its_worker() -> None:
    order, refund = _cancelled_click_order()
    message = _message(topics.REFUND_SUBMIT, order.pk)
    payment_services.mark_submitting(refund.pk)  # a stale worker claimed it after the last failure
    message.last_error = "timeout"
    tasks.on_refund_submit_dead(message)
    assert Refund.objects.get(pk=refund.pk).state == "submitting"
    assert not OrderEvent.objects.filter(order=order, type="refund_failed").exists()


def test_refund_transport_errors_dead_letter_as_failed_never_unknown() -> None:
    order, refund = _cancelled_click_order(refund="down")
    message = _dispatch_until_done(_message(topics.REFUND_SUBMIT, order.pk))
    assert message.status == "dead"
    refund.refresh_from_db()
    assert (refund.state, refund.error_code, refund.submitted_at) == ("failed", "dead_letter", None)
    event = OrderEvent.objects.get(order=order, type="refund_failed")
    assert event.data["state"] == "failed" and event.data["error_code"] == "dead_letter"
    tasks.on_refund_submit_dead(message)  # idempotent
    assert OrderEvent.objects.filter(order=order, type="refund_failed").count() == 1


def test_refund_handler_ignores_closed_and_manual_refunds() -> None:
    staff = admin()
    order = pay(place(fulfilment="pickup", provider="payme"))
    with pinned_clock(AT + timedelta(minutes=60)):
        services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
    refund = Refund.objects.get()
    payload = {"refund_id": refund.pk, "payment_id": refund.payment_id, "order_id": order.pk}
    tasks.submit_refund(payload)
    assert Refund.objects.get().state == "manual_required"
    with pytest.raises(LookupError):
        tasks.submit_refund({**payload, "refund_id": 10**9})


def test_handlers_and_hooks_are_registered() -> None:
    assert outbox._handlers[topics.BTS_CREATE_SHIPMENT] is tasks.handle_bts_create_shipment
    assert outbox._handlers[topics.BILLZ_PUSH_SALE] is tasks.handle_billz_push_sale
    assert outbox._handlers[topics.REFUND_SUBMIT] is tasks.handle_refund_submit
    assert outbox._dead_handlers[topics.BTS_CREATE_SHIPMENT] is tasks.on_bts_create_dead
    assert outbox._dead_handlers[topics.REFUND_SUBMIT] is tasks.on_refund_submit_dead
    message = OutboxMessage(topic=topics.REFUND_SUBMIT, payload=["not", "an", "object"])
    with pytest.raises(ValueError):
        tasks.handle_refund_submit(message)
