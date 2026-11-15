"""BTS invisibility of Yandex shipments (plan decision 3): in every state a Yandex shipment is ignored by the
BTS retry, the BTS create handler and its dead-letter hook, the BTS cancel branch, ``bts_failed``, K4, the
untracked-parcel check and the BTS poll; paying a Yandex order enqueues no ``bts.create_shipment``.
Manual transitions of a Yandex order exist only for an unaccounted parcel."""

from datetime import timedelta
from typing import Any
from unittest import mock

import pytest

from glowderma.commerce import selectors, services, state, tasks, topics
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import AT, admin, pay, place
from glowderma.core import enums
from glowderma.core.api.exceptions import InvalidTransition, NotCancellable
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import CourierClaim, Shipment
from glowderma.delivery.tests.factories import CourierClaimFactory
from glowderma.payments.models import Refund

pytestmark = pytest.mark.django_db

S = enums.ShipmentStage
FAR_FUTURE = AT + timedelta(days=365)

# (shipment state, stage, order status, claim open) — every state a Yandex shipment can be in
YANDEX_STATES = [
    ("not_created", S.AWAITING_DISPATCH, "processing", False),
    ("not_created", S.COURIER_REQUESTED, "processing", True),
    ("failed", S.FAILED, "processing", False),
    ("created", S.COURIER_ASSIGNED, "processing", True),
    ("created", S.IN_TRANSIT, "handed_to_courier", True),
    ("created", S.RETURNING, "handed_to_courier", True),
    ("cancelled", S.CANCELLED, "processing", False),
]


def yandex_order(**kwargs: Any) -> Order:
    """A delivery order placed through the services whose shipment is Yandex Standard (what checkout with a
    ``delivery_quote_id`` will write), still awaiting payment."""
    order = place(**kwargs)
    Shipment.objects.filter(order=order).update(provider="yandex", method="standard")
    return order


def put_in(order: Order, shipment_state: str, stage: str, status: str, claim_open: bool) -> Shipment:
    """Move a paid Yandex order's shipment (and the order) to the given state without any carrier."""
    shipment = Shipment.objects.get(order=order)
    claim = None
    if claim_open or shipment_state in ("created", "cancelled"):
        claim = CourierClaimFactory.create(
            shipment=shipment,
            status="accepted",
            accept_sent_at=AT,
            accepted_at=AT if shipment_state in ("created", "cancelled") else None,
            ended_at=None if claim_open else AT,
        )
    Shipment.objects.filter(pk=shipment.pk).update(
        state=shipment_state,
        stage=stage,
        claim=claim if shipment_state in ("created", "cancelled") else None,
        tariff=20_000 if shipment_state in ("created", "cancelled") else None,
        cancelled_at=AT if shipment_state == "cancelled" else None,
    )
    if status == "handed_to_courier":
        Order.objects.filter(pk=order.pk).update(status=status, handed_to_courier_at=AT + timedelta(hours=1))
    shipment.refresh_from_db()
    return shipment


def paid_yandex(shipment_state: str, stage: str, status: str, claim_open: bool) -> tuple[Order, Shipment]:
    order = pay(yandex_order())
    shipment = put_in(order, shipment_state, stage, status, claim_open)
    order.refresh_from_db()
    return order, shipment


def no_bts() -> tuple[Any, Any]:
    """Every BTS adapter lookup fails the test: the BTS port must not even be built for a Yandex order."""
    boom = mock.Mock(side_effect=AssertionError("BTS must not be called for a Yandex shipment"))
    return mock.patch("glowderma.commerce.tasks.get_bts", boom), mock.patch(
        "glowderma.delivery.services.get_bts", boom
    )


# --- payment -----------------------------------------------------------------------------------------------


def test_paying_a_yandex_order_enqueues_no_bts_create() -> None:
    order = pay(yandex_order())
    assert order.status == "processing"
    assert not OutboxMessage.objects.filter(topic=topics.BTS_CREATE_SHIPMENT).exists()
    shipment = Shipment.objects.get(order=order)
    assert (shipment.provider, shipment.state, shipment.stage) == (
        "yandex",
        "not_created",
        "awaiting_dispatch",
    )
    assert shipment.create_message is None and shipment.create_requests == 0
    assert OutboxMessage.objects.filter(topic=topics.PUSH_ORDER_STATUS, payload__status="processing").exists()
    # the BTS order beside it is unchanged: its parcel is requested at payment as before
    bts_order = pay(place())
    assert (
        OutboxMessage.objects.filter(topic=topics.BTS_CREATE_SHIPMENT, payload__order_id=bts_order.pk).count()
        == 1
    )


# --- BTS paths ignore a Yandex shipment in every state ------------------------------------------------------


@pytest.mark.parametrize(("shipment_state", "stage", "status", "claim_open"), YANDEX_STATES)
def test_bts_paths_ignore_a_yandex_shipment(
    shipment_state: str, stage: str, status: str, claim_open: bool
) -> None:
    staff = admin()
    order, shipment = paid_yandex(shipment_state, stage, status, claim_open)
    before = Shipment.objects.filter(pk=shipment.pk).values().get()
    tasks_patch, services_patch = no_bts()
    with tasks_patch, services_patch:
        with pytest.raises(InvalidTransition):
            services.retry_shipment(order.pk, staff.pk)
        payload = {"order_id": order.pk, "manual": False, "request_no": 1, "actor_id": None}
        tasks.create_shipment(payload)  # the BTS create handler: planned nothing
        message = OutboxMessage.objects.create(
            topic=topics.BTS_CREATE_SHIPMENT,
            payload=payload,
            dedupe_key=f"bts.create_shipment:{order.pk}:1",
            status="dead",
            last_error="boom",
        )
        tasks.on_bts_create_dead(message)
        assert tasks.run_bts_poll().tracked == []
        with pytest.raises(delivery_services.NotBtsShipment):
            services.apply_bts_status(order.pk, "200", "polling")
        with pytest.raises(delivery_services.NotBtsShipment):
            delivery_services.request_bts_create(order.pk, manual=True, actor_id=staff.pk)
    assert Shipment.objects.filter(pk=shipment.pk).values().get() == before
    assert not OrderEvent.objects.filter(
        order=order, type__in=["shipment_created", "shipment_failed"]
    ).exists()
    assert delivery_selectors.bts_failed_count() == 0
    assert delivery_selectors.stale_shipments(FAR_FUTURE) == []
    assert delivery_selectors.untracked_parcels(FAR_FUTURE, limit=10).ids == []
    assert selectors.unshipped_paid_orders(FAR_FUTURE, limit=10).ids == []
    detail = selectors.order_detail(order.pk)
    assert detail is not None
    assert detail.available_actions["retry_shipment"] is False
    assert detail.available_actions["transitions"] == []
    with pytest.raises(InvalidTransition):
        services.transition(
            order.pk, "delivered" if status == "handed_to_courier" else "handed_to_courier", staff.pk
        )


def test_the_bts_counterparts_are_still_seen() -> None:
    """Control: the same checks see a BTS order (so the empty answers above are the guard, not the setup)."""
    order = pay(place())
    Shipment.objects.filter(order=order).update(state="failed", attempts=1, last_attempt_at=AT)
    assert delivery_selectors.bts_failed_count() == 1
    assert selectors.unshipped_paid_orders(FAR_FUTURE, limit=10).ids == [order.pk]
    detail = selectors.order_detail(order.pk)
    assert detail is not None and detail.available_actions["transitions"] == ["handed_to_courier"]


# --- the BTS cancel branch ---------------------------------------------------------------------------------


def _cancel(order: Order) -> services.CancelResult:
    with pinned_clock(AT + timedelta(hours=2)):
        return services.cancel_order(order.pk, "Mijoz so‘rovi bilan", admin().pk)


@pytest.mark.parametrize(
    ("shipment_state", "stage", "status", "claim_open"),
    [
        ("not_created", S.AWAITING_DISPATCH, "processing", False),  # nothing summoned
        ("failed", S.FAILED, "processing", False),  # the courier search failed
        ("cancelled", S.CANCELLED, "processing", False),  # the claim was cancelled at Yandex
        ("created", S.RETURNING, "handed_to_courier", True),  # on its way back (plan §3.8)
        ("created", S.RETURNED, "handed_to_courier", False),
    ],
)
def test_a_yandex_order_without_a_courier_cancels_without_any_carrier_call(
    shipment_state: str, stage: str, status: str, claim_open: bool
) -> None:
    order, _shipment = paid_yandex(shipment_state, stage, status, claim_open)
    tasks_patch, services_patch = no_bts()
    with tasks_patch, services_patch:
        result = _cancel(order)
    assert result.bts_cancelled is False and result.refund_id is not None
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert Refund.objects.get(pk=result.refund_id).amount == order.total
    assert Shipment.objects.get(order=order).state == shipment_state  # the carrier side is not touched
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is False


@pytest.mark.parametrize(
    ("shipment_state", "stage", "status", "claim_open"),
    [
        ("not_created", S.COURIER_REQUESTED, "processing", True),  # a claim is being created / estimated
        ("created", S.COURIER_ASSIGNED, "processing", True),  # a courier is on the way
        ("created", S.IN_TRANSIT, "handed_to_courier", True),
        ("created", S.DELIVERED, "handed_to_courier", False),  # delivered, the order not yet moved
    ],
)
def test_a_yandex_order_with_a_courier_is_not_cancellable_yet(
    shipment_state: str, stage: str, status: str, claim_open: bool
) -> None:
    order, _shipment = paid_yandex(shipment_state, stage, status, claim_open)
    tasks_patch, services_patch = no_bts()
    with tasks_patch, services_patch, pytest.raises(NotCancellable):
        _cancel(order)
    order.refresh_from_db()
    assert order.status == status and not Refund.objects.exists()


# --- manual transitions ------------------------------------------------------------------------------------


def test_manual_delivered_only_while_a_parcel_is_unaccounted() -> None:
    staff = admin()
    order, _shipment = paid_yandex("created", S.CANCELLED, "handed_to_courier", False)
    with pytest.raises(InvalidTransition):
        services.transition(order.pk, "delivered", staff.pk)
    issue_id, _ = delivery_services.open_issue(
        order.pk,
        enums.DeliveryIssueKind.PARCEL_UNACCOUNTED,
        code="cancelled_with_items_on_hands",
        urgent=True,
        details={"order_id": order.pk},
    )
    detail = selectors.order_detail(order.pk)
    assert detail is not None and detail.available_actions["transitions"] == ["delivered"]
    with pinned_clock(AT + timedelta(hours=3)):
        services.transition(order.pk, "delivered", staff.pk)
    order.refresh_from_db()
    assert order.status == "delivered"
    assert OrderEvent.objects.get(order=order, type="status_changed").data["to"] == "delivered"
    assert delivery_selectors.issue(issue_id) is not None


def test_state_offers_nothing_for_a_processing_yandex_order() -> None:
    assert (
        state.manual_transitions("processing", "delivery", provider="yandex", parcel_unaccounted=True) == []
    )
    assert CourierClaim.objects.count() == 0
