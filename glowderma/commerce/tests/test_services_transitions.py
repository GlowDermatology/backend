"""Manual transitions vs state.py (03 §4.1 rows 8–10, 17), BTS statuses (rows 11–12), retry (row 13)."""

from datetime import timedelta

import pytest

from glowderma.commerce import selectors, services, state, topics
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, deliver, pay, place, ship, shopper
from glowderma.core import enums
from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import Shipment

pytestmark = pytest.mark.django_db
STATUSES = list(enums.OrderStatus.values)


def _order_in(status: str, fulfilment: str) -> Order:
    staff = admin()
    order = place(fulfilment=fulfilment)
    if status == "awaiting_payment":
        return order
    if status == "cancelled":
        with pinned_clock(AT + timedelta(minutes=5)):
            services.cancel_order(order.pk, "Mijoz so‘rovi bilan", staff.pk)
        order.refresh_from_db()
        return order
    order = ship(order) if fulfilment == "delivery" else pay(order)
    if status in ("handed_to_courier", "delivered") and fulfilment == "delivery":
        with pinned_clock(AT + timedelta(hours=2)):
            services.transition(order.pk, "handed_to_courier", staff.pk)
    if status == "delivered":
        with pinned_clock(AT + timedelta(hours=3)):
            services.transition(order.pk, "delivered", staff.pk)
    order.refresh_from_db()
    return order


CASES = [
    (status, fulfilment)
    for status in STATUSES
    for fulfilment in ("delivery", "pickup")
    if not (status == "handed_to_courier" and fulfilment == "pickup")
]


@pytest.mark.parametrize(("status", "fulfilment"), CASES)
def test_transition_matrix_matches_state_py(status: str, fulfilment: str) -> None:
    order = _order_in(status, fulfilment)
    assert order.status == status
    allowed = state.manual_transitions(status, fulfilment)
    staff = admin()
    for target in STATUSES:
        if target in allowed:
            continue
        with pytest.raises(InvalidTransition) as caught, pinned_clock(AT + timedelta(hours=4)):
            services.transition(order.pk, target, staff.pk)
        assert str(caught.value.detail) == "Bu holatga o‘tib bo‘lmaydi."
    assert Order.objects.get(pk=order.pk).status == status
    for target in allowed:
        with pinned_clock(AT + timedelta(hours=5)):
            services.transition(order.pk, target, staff.pk)
        order.refresh_from_db()
        assert order.status == target
        break


def test_transition_effects() -> None:
    staff = admin()
    order = ship(place())
    moved = AT + timedelta(hours=2)
    with pinned_clock(moved):
        services.transition(order.pk, "handed_to_courier", staff.pk)
    order.refresh_from_db()
    assert (order.handed_to_courier_at, order.status_changed_at) == (moved, moved)
    event = OrderEvent.objects.get(order=order, type="status_changed")
    assert (event.source, event.actor_id, event.status_after) == ("admin", staff.pk, "handed_to_courier")
    assert event.data == {"from": "processing", "to": "handed_to_courier", "push_sent": True}
    push = OutboxMessage.objects.get(dedupe_key=f"push.order_status:{order.pk}:handed_to_courier")
    assert push.payload["event_id"] == event.pk
    audit = AuditEntry.objects.get(action="order.status_changed")
    assert audit.after == {"from": "processing", "to": "handed_to_courier"}
    with pinned_clock(moved + timedelta(hours=20)):
        services.transition(order.pk, "delivered", staff.pk)
    order.refresh_from_db()
    assert order.delivered_at == moved + timedelta(hours=20)


def test_bts_statuses_move_the_order_and_are_deduplicated() -> None:
    order = ship(place(customer=shopper(device=False)))
    with pinned_clock(AT + timedelta(minutes=30)):
        assert not services.apply_bts_status(order.pk, "100", "polling")  # accepted: no order change
    with pinned_clock(AT + timedelta(hours=2)):
        assert services.apply_bts_status(order.pk, "200", "webhook", payload={"id": 1})
        assert not services.apply_bts_status(order.pk, "200", "webhook", payload={"id": 1})  # duplicate
    with pinned_clock(AT + timedelta(hours=26)):
        assert services.apply_bts_status(order.pk, "300", "webhook")
        assert not services.apply_bts_status(order.pk, "200", "polling")  # terminal: recorded only
    order.refresh_from_db()
    assert order.status == "delivered" and order.delivered_at == AT + timedelta(hours=26)
    events = list(OrderEvent.objects.filter(order=order, source="bts").order_by("id"))
    assert [(e.type, e.status_after, e.data) for e in events] == [
        (
            "handed_to_courier",
            "handed_to_courier",
            {"via": "webhook", "bts_status": "200", "push_sent": False},
        ),
        ("delivered", "delivered", {"via": "webhook", "bts_status": "300", "push_sent": False}),
    ]
    shipment = Shipment.objects.get(order=order)
    assert (shipment.bts_status, shipment.via) == ("200", "polling")
    assert shipment.events.filter(kind="status_received").count() == 4


def test_delivered_straight_from_processing() -> None:
    order = ship(place())
    with pinned_clock(AT + timedelta(hours=26)):
        assert services.apply_bts_status(order.pk, "300", "polling")
    assert Order.objects.get(pk=order.pk).status == "delivered"


def test_retry_shipment_precondition_pending_and_effect() -> None:
    staff = admin()
    order = pay(place(fake={"bts": {"fail_attempts": 1}}))
    with pytest.raises(InvalidTransition) as refused:
        services.retry_shipment(order.pk, staff.pk)  # not failed yet (the automatic create is pending)
    assert str(refused.value.detail) == "BTS ga qayta yuborib bo‘lmaydi."
    with pinned_clock(AT + timedelta(minutes=4)):
        deliver(topics.BTS_CREATE_SHIPMENT, order.pk)  # automatic attempt fails
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.attempts, shipment.last_error_code) == ("failed", 1, "address_not_found")
    detail = selectors.order_detail(order.pk)
    assert detail is not None and detail.available_actions["retry_shipment"] is True
    with pinned_clock(AT + timedelta(minutes=30)):
        services.retry_shipment(order.pk, staff.pk)
    message = Shipment.objects.get(order=order).create_message
    assert message is not None and message.payload == {
        "order_id": order.pk,
        "manual": True,
        "request_no": 2,
        "actor_id": staff.pk,
    }
    assert message.dedupe_key == f"bts.create_shipment:{order.pk}:2"
    with pytest.raises(Conflict) as busy:
        services.retry_shipment(order.pk, staff.pk)
    assert str(busy.value.detail) == "BTS so‘rovi allaqachon yuborilmoqda."
    assert AuditEntry.objects.filter(action="order.shipment_retry_requested").count() == 1
    with pinned_clock(AT + timedelta(minutes=31)):
        deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    created = OrderEvent.objects.get(order=order, type="shipment_created")
    assert (created.source, created.actor_id) == ("admin", staff.pk)
    assert created.data == {
        "tracking_number": str(48_200_000 + order.pk % 100_000),
        "tariff": 25_000,
        "manual": True,
    }
    with pytest.raises(InvalidTransition):
        services.retry_shipment(order.pk, staff.pk)  # created now


def test_retry_shipment_refuses_pickup_and_unknown_orders() -> None:
    staff = admin()
    with pytest.raises(InvalidTransition):
        services.retry_shipment(pay(place(fulfilment="pickup")).pk, staff.pk)
    with pytest.raises(services.OrderNotFound):
        services.retry_shipment(10**9, staff.pk)
