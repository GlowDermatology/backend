"""The order state machine as data (design 03 §4.1 / §4.2; contract §2.2).

B5's ``commerce.services`` (``checkout``, ``confirm_payment_effects``, ``cancel_order``, ``transition``,
``apply_bts_status``, ``expire_reservation``) consume these tables; nothing here touches the database. Each
:class:`Transition` is one status-changing row of §4.1 (rows that keep the status — retry-shipment, BTS
create, refund handlers, provider failure callbacks — write non-status events and are not transitions).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from glowderma.core import enums


class Trigger(StrEnum):
    """Who or what drives a transition (the §4.1 "Trigger" column, normalised)."""

    CHECKOUT = "checkout"  # M2 POST /checkout → services.checkout
    PAYMENT_CAPTURE = "payment_capture"  # Click complete / Payme PerformTransaction → confirm_payment_effects
    RESERVATION_EXPIRY = "reservation_expiry"  # beat commerce.expire_reservations → expire_reservation
    ADMIN_CANCEL = "admin_cancel"  # POST orders/{id}/cancel/ → cancel_order
    CUSTOMER_CANCEL = "customer_cancel"  # M2 POST /orders/{id}/cancel (C-6, optional)
    ADMIN_TRANSITION = "admin_transition"  # POST orders/{id}/transition/ → transition
    BTS_STATUS = "bts_status"  # BTS webhook / commerce.poll_bts → apply_bts_status


@dataclass(frozen=True, slots=True)
class Transition:
    """One status-changing row of design 03 §4.1."""

    row: int
    source: str | None  # OrderStatus; None = the order is created by this transition
    target: str  # OrderStatus
    trigger: Trigger
    event_type: str  # the status-changing OrderEvent (status_after == target)
    event_source: str  # EventSource of that event
    fulfilment: str | None = None  # FulfilmentMethod the row is restricted to, None = any
    cancellation_reason: str = ""  # CancellationReason written on the order (cancel rows only)
    push: bool = False  # push.order_status enqueued (dedupe push.order_status:{order_id}:{target})
    audit_action: str = ""  # core audit action recorded by the row ("" = system row, no audit)
    bts_cancel: bool = False  # the synchronous BTS cancel precedes every write (rows 6/7)


_S = enums.OrderStatus
_F = enums.FulfilmentMethod
_E = enums.OrderEventType
_SRC = enums.EventSource
_R = enums.CancellationReason

AUDIT_ORDER_CANCELLED = "order.cancelled"
AUDIT_ORDER_STATUS_CHANGED = "order.status_changed"
AUDIT_SHIPMENT_RETRY_REQUESTED = "order.shipment_retry_requested"

TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        row=1,
        source=None,
        target=_S.AWAITING_PAYMENT,
        trigger=Trigger.CHECKOUT,
        event_type=_E.ORDER_CREATED,
        event_source=_SRC.SYSTEM,
    ),
    Transition(
        row=2,
        source=_S.AWAITING_PAYMENT,
        target=_S.PROCESSING,
        trigger=Trigger.PAYMENT_CAPTURE,
        event_type=_E.PAYMENT_CONFIRMED,
        event_source=_SRC.SYSTEM,
        push=True,
    ),
    Transition(  # no push (N-1), no audit
        row=3,
        source=_S.AWAITING_PAYMENT,
        target=_S.CANCELLED,
        trigger=Trigger.RESERVATION_EXPIRY,
        event_type=_E.RESERVATION_EXPIRED,
        event_source=_SRC.SYSTEM,
        cancellation_reason=_R.RESERVATION_EXPIRED,
    ),
    Transition(
        row=4,
        source=_S.AWAITING_PAYMENT,
        target=_S.CANCELLED,
        trigger=Trigger.ADMIN_CANCEL,
        event_type=_E.CANCELLED,
        event_source=_SRC.ADMIN,
        cancellation_reason=_R.ADMIN,
        push=True,
        audit_action=AUDIT_ORDER_CANCELLED,
    ),
    Transition(
        row=5,
        source=_S.AWAITING_PAYMENT,
        target=_S.CANCELLED,
        trigger=Trigger.CUSTOMER_CANCEL,
        event_type=_E.CANCELLED,
        event_source=_SRC.SYSTEM,
        cancellation_reason=_R.CUSTOMER_REQUEST,
        push=True,
    ),
    Transition(
        row=6,
        source=_S.PROCESSING,
        target=_S.CANCELLED,
        trigger=Trigger.ADMIN_CANCEL,
        event_type=_E.CANCELLED,
        event_source=_SRC.ADMIN,
        cancellation_reason=_R.ADMIN,
        push=True,
        audit_action=AUDIT_ORDER_CANCELLED,
        bts_cancel=True,
    ),
    Transition(
        row=7,
        source=_S.HANDED_TO_COURIER,
        target=_S.CANCELLED,
        trigger=Trigger.ADMIN_CANCEL,
        event_type=_E.CANCELLED,
        event_source=_SRC.ADMIN,
        cancellation_reason=_R.ADMIN,
        push=True,
        audit_action=AUDIT_ORDER_CANCELLED,
        bts_cancel=True,
    ),
    Transition(
        row=8,
        source=_S.PROCESSING,
        target=_S.HANDED_TO_COURIER,
        trigger=Trigger.ADMIN_TRANSITION,
        event_type=_E.STATUS_CHANGED,
        event_source=_SRC.ADMIN,
        fulfilment=_F.DELIVERY,
        push=True,
        audit_action=AUDIT_ORDER_STATUS_CHANGED,
    ),
    Transition(
        row=9,
        source=_S.PROCESSING,
        target=_S.DELIVERED,
        trigger=Trigger.ADMIN_TRANSITION,
        event_type=_E.STATUS_CHANGED,
        event_source=_SRC.ADMIN,
        fulfilment=_F.PICKUP,
        push=True,
        audit_action=AUDIT_ORDER_STATUS_CHANGED,
    ),
    Transition(
        row=10,
        source=_S.HANDED_TO_COURIER,
        target=_S.DELIVERED,
        trigger=Trigger.ADMIN_TRANSITION,
        event_type=_E.STATUS_CHANGED,
        event_source=_SRC.ADMIN,
        push=True,
        audit_action=AUDIT_ORDER_STATUS_CHANGED,
    ),
    Transition(
        row=11,
        source=_S.PROCESSING,
        target=_S.HANDED_TO_COURIER,
        trigger=Trigger.BTS_STATUS,
        event_type=_E.HANDED_TO_COURIER,
        event_source=_SRC.BTS,
        fulfilment=_F.DELIVERY,
        push=True,
    ),
    Transition(
        row=12,
        source=_S.PROCESSING,
        target=_S.DELIVERED,
        trigger=Trigger.BTS_STATUS,
        event_type=_E.DELIVERED,
        event_source=_SRC.BTS,
        fulfilment=_F.DELIVERY,
        push=True,
    ),
    Transition(
        row=12,
        source=_S.HANDED_TO_COURIER,
        target=_S.DELIVERED,
        trigger=Trigger.BTS_STATUS,
        event_type=_E.DELIVERED,
        event_source=_SRC.BTS,
        fulfilment=_F.DELIVERY,
        push=True,
    ),
)

TERMINAL_STATUSES: frozenset[str] = frozenset({_S.DELIVERED, _S.CANCELLED})  # §4.1 row 17
CANCELLABLE_STATUSES: frozenset[str] = frozenset(
    {_S.AWAITING_PAYMENT, _S.PROCESSING, _S.HANDED_TO_COURIER}
)  # §4.2 ``cancel``
PAID_STATUSES: frozenset[str] = frozenset({_S.PROCESSING, _S.HANDED_TO_COURIER, _S.DELIVERED})

# The timestamp column ``commerce.services`` sets together with ``status``/``status_changed_at`` (03 §1.3).
STATUS_TIMESTAMP_FIELD: dict[str, str] = {
    _S.PROCESSING: "paid_at",
    _S.HANDED_TO_COURIER: "handed_to_courier_at",
    _S.DELIVERED: "delivered_at",
    _S.CANCELLED: "cancelled_at",
}

# Statuses that have a push template (contract ``TemplateStatus``); ``awaiting_payment`` never pushes.
PUSHABLE_STATUSES: frozenset[str] = frozenset(enums.TemplateStatus.values)

# Request-level refusals of §4.1 row 17 / §4.2 (contract error codes).
CANCEL_REFUSED = "not_cancellable"
TRANSITION_REFUSED = "invalid_transition"
RETRY_SHIPMENT_REFUSED = "invalid_transition"
RETRY_SHIPMENT_PENDING = "conflict"


def transitions_from(source: str | None, trigger: Trigger | None = None) -> tuple[Transition, ...]:
    """Rows leaving ``source`` (``None`` = creation), optionally restricted to one trigger."""
    return tuple(t for t in TRANSITIONS if t.source == source and (trigger is None or t.trigger == trigger))


def find_transition(
    source: str | None, target: str, trigger: Trigger, *, fulfilment: str
) -> Transition | None:
    """The §4.1 row for ``source → target`` under ``trigger`` for an order with ``fulfilment``.

    ``fulfilment`` is required (every order has one) so that the rows §4.1 restricts by fulfilment
    (8, 9, 11, 12) can never match the wrong order: a missing keyword is a ``TypeError``, not a wildcard.
    """
    for transition in TRANSITIONS:
        if transition.source != source or transition.target != target or transition.trigger != trigger:
            continue
        if transition.fulfilment is None or transition.fulfilment == fulfilment:
            return transition
    return None


def manual_transitions(status: str, fulfilment: str) -> list[str]:
    """``available_actions.transitions`` (§4.2): the admin-driven targets offered for ``status``."""
    return [
        t.target
        for t in TRANSITIONS
        if t.trigger == Trigger.ADMIN_TRANSITION
        and t.source == status
        and (t.fulfilment is None or t.fulfilment == fulfilment)
    ]


def can_cancel(status: str) -> bool:
    """``available_actions.cancel``; the BTS rule is checked at POST time, not here."""
    return status in CANCELLABLE_STATUSES


def can_retry_shipment(
    fulfilment: str, status: str, shipment_state: str | None, shipment_pending: bool
) -> bool:
    """``available_actions.retry_shipment`` (§4.2): delivery ∧ processing ∧ shipment failed ∧ ¬pending."""
    return (
        fulfilment == _F.DELIVERY
        and status == _S.PROCESSING
        and shipment_state == enums.ShipmentState.FAILED
        and not shipment_pending
    )


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
