"""The order state machine data (design 03 §4) and the outbox topic registry (§5)."""

import pytest

from glowderma.commerce import state, topics
from glowderma.commerce.models import EVENT_SOURCES, EVENT_STATUS_AFTER
from glowderma.commerce.state import Transition, Trigger
from glowderma.core import enums

S = enums.OrderStatus
F = enums.FulfilmentMethod


def test_transition_table_matches_design_rows() -> None:
    rows = [(t.row, t.source, t.target, t.trigger) for t in state.TRANSITIONS]
    assert rows == [
        (1, None, "awaiting_payment", Trigger.CHECKOUT),
        (2, "awaiting_payment", "processing", Trigger.PAYMENT_CAPTURE),
        (3, "awaiting_payment", "cancelled", Trigger.RESERVATION_EXPIRY),
        (4, "awaiting_payment", "cancelled", Trigger.ADMIN_CANCEL),
        (5, "awaiting_payment", "cancelled", Trigger.CUSTOMER_CANCEL),
        (6, "processing", "cancelled", Trigger.ADMIN_CANCEL),
        (7, "handed_to_courier", "cancelled", Trigger.ADMIN_CANCEL),
        (8, "processing", "handed_to_courier", Trigger.ADMIN_TRANSITION),
        (9, "processing", "delivered", Trigger.ADMIN_TRANSITION),
        (10, "handed_to_courier", "delivered", Trigger.ADMIN_TRANSITION),
        (11, "processing", "handed_to_courier", Trigger.BTS_STATUS),
        (12, "processing", "delivered", Trigger.BTS_STATUS),
        (12, "handed_to_courier", "delivered", Trigger.BTS_STATUS),
    ]
    # manual transitions never go backwards and nothing leaves a terminal status (§4.1 row 17)
    assert not [t for t in state.TRANSITIONS if t.source in state.TERMINAL_STATUSES]
    assert {t.target for t in state.TRANSITIONS} | {None} >= set(S.values) - {None}


@pytest.mark.parametrize("transition", state.TRANSITIONS, ids=lambda t: f"row{t.row}-{t.target}")
def test_every_transition_event_satisfies_the_orderevent_checks(transition: Transition) -> None:
    assert transition.target in EVENT_STATUS_AFTER[transition.event_type]
    assert transition.event_source in EVENT_SOURCES[transition.event_type]
    assert transition.target in state.STATUS_TIMESTAMP_FIELD or transition.source is None
    if transition.target == S.CANCELLED:
        assert transition.cancellation_reason in enums.CancellationReason.values
    else:
        assert transition.cancellation_reason == ""
    if transition.push:
        assert transition.target in state.PUSHABLE_STATUSES
    if transition.event_source == enums.EventSource.ADMIN:
        assert transition.audit_action
    else:
        assert transition.audit_action == ""


def test_cancel_rows_carry_the_contract_rules() -> None:
    admin_cancels = state.transitions_from(None)  # creation only
    assert [t.row for t in admin_cancels] == [1]
    assert [t.row for t in state.transitions_from(S.AWAITING_PAYMENT)] == [2, 3, 4, 5]
    assert [t.row for t in state.transitions_from(S.PROCESSING, Trigger.ADMIN_CANCEL)] == [6]
    expiry = state.find_transition(
        S.AWAITING_PAYMENT, S.CANCELLED, Trigger.RESERVATION_EXPIRY, fulfilment=F.DELIVERY
    )
    assert expiry is not None and not expiry.push and expiry.audit_action == ""  # N-1
    assert expiry.cancellation_reason == enums.CancellationReason.RESERVATION_EXPIRED
    paid_cancel = state.find_transition(S.PROCESSING, S.CANCELLED, Trigger.ADMIN_CANCEL, fulfilment=F.PICKUP)
    assert paid_cancel is not None and paid_cancel.bts_cancel and paid_cancel.push
    unpaid_cancel = state.find_transition(
        S.AWAITING_PAYMENT, S.CANCELLED, Trigger.ADMIN_CANCEL, fulfilment=F.DELIVERY
    )
    assert unpaid_cancel is not None and not unpaid_cancel.bts_cancel
    customer = state.find_transition(
        S.AWAITING_PAYMENT, S.CANCELLED, Trigger.CUSTOMER_CANCEL, fulfilment=F.DELIVERY
    )
    assert customer is not None and customer.event_source == enums.EventSource.SYSTEM
    assert (
        state.find_transition(S.PROCESSING, S.CANCELLED, Trigger.CUSTOMER_CANCEL, fulfilment=F.DELIVERY)
        is None
    )  # C-6


def test_find_transition_respects_fulfilment() -> None:
    assert (
        state.find_transition(S.PROCESSING, S.DELIVERED, Trigger.ADMIN_TRANSITION, fulfilment=F.DELIVERY)
        is None
    )
    row9 = state.find_transition(S.PROCESSING, S.DELIVERED, Trigger.ADMIN_TRANSITION, fulfilment=F.PICKUP)
    assert row9 is not None and row9.row == 9
    row8 = state.find_transition(
        S.PROCESSING, S.HANDED_TO_COURIER, Trigger.ADMIN_TRANSITION, fulfilment=F.DELIVERY
    )
    assert row8 is not None and row8.row == 8
    assert (
        state.find_transition(
            S.PROCESSING, S.HANDED_TO_COURIER, Trigger.ADMIN_TRANSITION, fulfilment=F.PICKUP
        )
        is None
    )
    assert (
        state.find_transition(
            S.AWAITING_PAYMENT, S.HANDED_TO_COURIER, Trigger.ADMIN_TRANSITION, fulfilment=F.DELIVERY
        )
        is None
    )
    assert (
        state.find_transition(S.DELIVERED, S.PROCESSING, Trigger.ADMIN_TRANSITION, fulfilment=F.DELIVERY)
        is None
    )
    assert (
        state.find_transition(S.PROCESSING, S.HANDED_TO_COURIER, Trigger.BTS_STATUS, fulfilment=F.PICKUP)
        is None
    )


@pytest.mark.parametrize("fulfilment", F.values)
def test_find_transition_never_treats_a_missing_fulfilment_as_a_wildcard(fulfilment: str) -> None:
    """Regression: rows 8/9/11/12 must never match an order whose fulfilment the caller forgot to pass."""
    with pytest.raises(TypeError):
        state.find_transition(S.PROCESSING, S.DELIVERED, Trigger.ADMIN_TRANSITION)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        state.find_transition(S.PROCESSING, S.DELIVERED, Trigger.ADMIN_TRANSITION, None)  # type: ignore[arg-type,call-arg]
    # every fulfilment-restricted row answers only for its own fulfilment
    for transition in state.TRANSITIONS:
        if transition.fulfilment is None:
            continue
        found = state.find_transition(
            transition.source, transition.target, transition.trigger, fulfilment=fulfilment
        )
        if fulfilment == transition.fulfilment:
            assert found is transition
        else:
            assert found is None or found.fulfilment == fulfilment
    # ...and find_transition agrees with manual_transitions for every (status, fulfilment) pair
    for status in S.values:
        offered = state.manual_transitions(status, fulfilment)
        for target in S.values:
            row = state.find_transition(status, target, Trigger.ADMIN_TRANSITION, fulfilment=fulfilment)
            assert (row is not None) == (target in offered), (status, target, fulfilment)


def test_available_actions_derivation() -> None:
    assert state.manual_transitions(S.PROCESSING, F.DELIVERY) == ["handed_to_courier"]
    assert state.manual_transitions(S.PROCESSING, F.PICKUP) == ["delivered"]
    assert state.manual_transitions(S.HANDED_TO_COURIER, F.DELIVERY) == ["delivered"]  # S-2: the rule wins
    assert state.manual_transitions(S.HANDED_TO_COURIER, F.PICKUP) == ["delivered"]
    for status in (S.AWAITING_PAYMENT, S.DELIVERED, S.CANCELLED):
        assert state.manual_transitions(status, F.DELIVERY) == []
    assert [state.can_cancel(s) for s in S.values] == [True, True, True, False, False]
    assert [state.is_terminal(s) for s in S.values] == [False, False, False, True, True]
    assert state.can_retry_shipment(F.DELIVERY, S.PROCESSING, "failed", False)
    assert not state.can_retry_shipment(F.DELIVERY, S.PROCESSING, "failed", True)
    assert not state.can_retry_shipment(F.DELIVERY, S.PROCESSING, "created", False)
    assert not state.can_retry_shipment(F.DELIVERY, S.PROCESSING, None, False)
    assert not state.can_retry_shipment(F.DELIVERY, S.HANDED_TO_COURIER, "failed", False)
    assert not state.can_retry_shipment(F.PICKUP, S.PROCESSING, "failed", False)


def test_status_tables_cover_the_enum() -> None:
    assert set(state.STATUS_TIMESTAMP_FIELD) == set(S.values) - {S.AWAITING_PAYMENT}
    assert state.PUSHABLE_STATUSES == set(S.values) - {S.AWAITING_PAYMENT}
    assert state.PAID_STATUSES | {S.AWAITING_PAYMENT, S.CANCELLED} == set(S.values)
    assert state.CANCELLABLE_STATUSES.isdisjoint(state.TERMINAL_STATUSES)
    assert state.CANCEL_REFUSED == "not_cancellable" and state.TRANSITION_REFUSED == "invalid_transition"
    assert state.RETRY_SHIPMENT_PENDING == "conflict"
    assert state.AUDIT_SHIPMENT_RETRY_REQUESTED == "order.shipment_retry_requested"


def test_outbox_topic_registry() -> None:
    assert topics.HANDLED_TOPICS == (
        "bts.create_shipment",
        "billz.push_sale",
        "refund.submit",
        "billz.stock_reread",  # gap X-STOCK-FRESHNESS
    )
    assert topics.PRODUCED_TOPICS == ("billz.push_sale", "push.order_status", "billz.stock_reread")
    assert topics.DEAD_LETTER_TOPICS == ("bts.create_shipment", "refund.submit")
    assert set(topics.PAYLOAD_KEYS) == set(topics.HANDLED_TOPICS) | set(topics.PRODUCED_TOPICS)
    assert topics.bts_create_shipment_key(10250, 2) == "bts.create_shipment:10250:2"
    assert topics.billz_push_sale_key("0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10") == (
        "billz.push_sale:0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10"
    )
    assert topics.refund_submit_key(7) == "refund.submit:7"
    assert topics.billz_stock_reread_key(10248) == "billz.stock_reread:10248"
    assert (
        topics.push_order_status_key(10248, "handed_to_courier")
        == "push.order_status:10248:handed_to_courier"
    )
    assert topics.order_status_inbox_key(10248, "handed_to_courier") == "order_status:10248:handed_to_courier"
    assert topics.DEAD_LETTER_ERROR_CODE == "dead_letter"
