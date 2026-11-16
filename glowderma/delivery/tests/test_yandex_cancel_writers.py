"""The delivery writers of the carrier-side cancel and returns (plan §3.8): the cancel stamp, a withdrawn
claim, a paid cancellation's fee counted once whatever arrives first (the fee or Yandex's final price), the
confirmed return (stage, event, a lagging ``returning`` that never moves the parcel back on the road), the
shipment of a claim we cancelled, and the selectors the cancel decides from."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.delivery import selectors, services
from glowderma.delivery.models import CourierClaim, Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import CourierClaimFactory, ShipmentFactory, yandex_shipment
from glowderma.integrations.yandex_delivery.ports import Claim

pytestmark = pytest.mark.django_db

G = enums.ShipmentStage
PAID_FEE = 22_000


def _accepted(status: str = "performer_found", stage: str = G.COURIER_ASSIGNED, **fields: Any) -> Shipment:
    return yandex_shipment(state=Shipment.State.CREATED, stage=stage, claim_status=status, **fields)


def _snapshot(claim: CourierClaim, status: str, *, minutes: int = 1, **fields: Any) -> Claim:
    return Claim(
        claim_id=claim.claim_id,
        status=status,
        version=(claim.version or 1) + 1,
        updated_ts=timeutils.now() + timedelta(minutes=minutes),
        **fields,
    )


def _record(shipment: Shipment, snapshot: Claim) -> services.ClaimUpdate:
    return services.record_claim_snapshot(
        shipment.order_id, snapshot, via=enums.ShipmentVia.POLLING, paid_fee=PAID_FEE, at=timeutils.now()
    )


def test_the_cancel_stamp_is_set_and_cleared() -> None:
    shipment = _accepted()
    first = timeutils.now() - timedelta(minutes=5)
    assert services.request_carrier_cancel(shipment.order_id, at=first) is True
    shipment.refresh_from_db()
    assert shipment.cancel_requested_at == first
    # a repeated cancel resumes the first one: the review beat's 10 minutes run from the first request
    assert services.request_carrier_cancel(shipment.order_id, at=timeutils.now()) is False
    shipment.refresh_from_db()
    assert shipment.cancel_requested_at == first
    assert services.clear_carrier_cancel(shipment.order_id) is True
    assert services.clear_carrier_cancel(shipment.order_id) is False
    shipment.refresh_from_db()
    assert shipment.cancel_requested_at is None


def test_a_withdrawn_claim_is_closed_settled_and_shows_cancelled() -> None:
    shipment = yandex_shipment(stage=G.COURIER_REQUESTED)
    claim = CourierClaimFactory.create(shipment=shipment, claim_id="")
    Shipment.objects.filter(pk=shipment.pk).update(claim=claim)
    assert services.withdraw_claim(shipment.order_id, claim.pk, at=timeutils.now()) is True
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.settled_at is not None
    assert claim.error_code == services.CANCEL_WITHDRAWN
    shipment.refresh_from_db()
    assert shipment.stage == G.CANCELLED and shipment.state == Shipment.State.NOT_CREATED
    assert services.withdraw_claim(shipment.order_id, claim.pk, at=timeutils.now()) is False


def test_a_cancel_fee_recorded_first_is_replaced_by_the_final_price() -> None:
    shipment = _accepted(status="pickup_arrived", stage=G.COURIER_ARRIVED)
    claim = shipment.claim
    assert claim is not None
    _record(shipment, _snapshot(claim, "cancelled_with_payment"))
    assert services.record_cancel_fee(shipment.order_id, claim.pk, 9_000, paid_fee=PAID_FEE) is True
    assert services.record_cancel_fee(shipment.order_id, claim.pk, 9_000, paid_fee=PAID_FEE) is False
    shipment.refresh_from_db()
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (9_000, PAID_FEE - 9_000)
    claim.refresh_from_db()
    update = _record(shipment, _snapshot(claim, "cancelled_with_payment", minutes=2, final_price=9_500))
    assert update.final_price_added == 9_500
    shipment.refresh_from_db()
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (9_500, PAID_FEE - 9_500)


def test_a_cancel_fee_after_the_final_price_adds_nothing() -> None:
    shipment = _accepted(status="pickup_arrived", stage=G.COURIER_ARRIVED)
    claim = shipment.claim
    assert claim is not None
    _record(shipment, _snapshot(claim, "cancelled_with_payment", final_price=9_000))
    assert services.record_cancel_fee(shipment.order_id, claim.pk, 9_000, paid_fee=PAID_FEE) is True
    shipment.refresh_from_db()
    assert shipment.carrier_cost_total == 9_000
    claim.refresh_from_db()
    assert claim.cancel_fee == 9_000
    with pytest.raises(ValueError):
        services.record_cancel_fee(shipment.order_id, claim.pk, -1, paid_fee=PAID_FEE)


def test_a_sent_cancel_and_a_sent_return_are_recorded_once() -> None:
    shipment = _accepted(status="pickup_arrived", stage=G.COURIER_ARRIVED)
    claim = shipment.claim
    assert claim is not None
    sent = timeutils.now() - timedelta(minutes=2)
    assert services.record_cancel_sent(shipment.order_id, claim.pk, cancel_state="paid", at=sent) is True
    later = timeutils.now()
    assert services.record_cancel_sent(shipment.order_id, claim.pk, cancel_state="free", at=later) is False
    claim.refresh_from_db()
    assert (claim.cancel_sent_at, claim.cancel_state) == (sent, "paid")  # the first send is kept
    row = selectors.claim_row(claim.pk)
    assert row is not None and (row.cancel_sent_at, row.cancel_state) == (sent, "paid")
    assert not selectors.return_requested(shipment.pk, claim.claim_id)
    assert services.record_return_request(shipment.order_id, claim.pk, at=later) is True
    assert services.record_return_request(shipment.order_id, claim.pk, at=later) is False
    assert selectors.return_requested(shipment.pk, claim.claim_id)
    assert not selectors.return_requested(shipment.pk, "")
    events = ShipmentEvent.objects.filter(shipment=shipment, kind=ShipmentEvent.Kind.RETURN_REQUESTED)
    assert [event.payload for event in events] == [{"claim_id": claim.claim_id, "claim_number": 1}]


def _paid_cancel_sent(price: int | None = 9_000, state: str = "paid") -> tuple[Shipment, CourierClaim]:
    shipment = _accepted(status="pickup_arrived", stage=G.COURIER_ARRIVED)
    claim = shipment.claim
    assert claim is not None
    at = timeutils.now()
    assert services.record_cancel_sent(shipment.order_id, claim.pk, cancel_state=state, price=price, at=at)
    claim.refresh_from_db()
    return shipment, claim


def test_a_lost_paid_cancel_counts_its_quoted_price_once_yandex_shows_it() -> None:
    shipment, claim = _paid_cancel_sent()
    assert (claim.cancel_state, claim.cancel_price, claim.cancel_fee) == ("paid", 9_000, None)
    later = timeutils.now()
    assert not services.record_cancel_sent(  # the first terms and price are kept
        shipment.order_id, claim.pk, cancel_state="paid", price=1, at=later
    )
    _record(shipment, _snapshot(claim, "pickup_arrived"))  # claims/info lags: nothing is counted
    shipment.refresh_from_db()
    assert shipment.carrier_cost_total == 0
    claim.refresh_from_db()
    _record(shipment, _snapshot(claim, "cancelled_with_payment", minutes=2))
    claim.refresh_from_db()
    assert (claim.cancel_price, claim.cancel_fee, claim.final_price) == (9_000, 9_000, None)
    shipment.refresh_from_db()
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (9_000, PAID_FEE - 9_000)
    # every later path finds it counted: the writer again, txn B, the answered fee
    _record(shipment, _snapshot(claim, "cancelled_with_payment", minutes=3))
    assert services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE) == 9_000
    assert services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE, quoted=8_000) == 9_000
    assert services.record_cancel_fee(shipment.order_id, claim.pk, 9_000, paid_fee=PAID_FEE) is False
    shipment.refresh_from_db()
    assert shipment.carrier_cost_total == 9_000
    claim.refresh_from_db()
    _record(shipment, _snapshot(claim, "cancelled_with_payment", minutes=4, final_price=9_400))
    shipment.refresh_from_db()  # the final price replaces the fee
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (9_400, PAID_FEE - 9_400)


def test_a_quoted_fee_shown_with_the_final_price_counts_the_final_price_only() -> None:
    shipment, claim = _paid_cancel_sent()
    _record(shipment, _snapshot(claim, "cancelled_with_payment", final_price=9_200))
    claim.refresh_from_db()
    assert (claim.cancel_fee, claim.final_price, claim.settled_at is not None) == (9_000, 9_200, True)
    shipment.refresh_from_db()
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (9_200, PAID_FEE - 9_200)


@pytest.mark.parametrize(
    ("state", "price", "status"),
    [
        ("free", 9_000, "cancelled_with_payment"),  # a free cancel stores no price
        ("paid", None, "cancelled_with_payment"),  # cancel-info gave no price
        ("paid", 9_000, "cancelled"),  # Yandex ended it free
        ("paid", 9_000, "cancelled_by_taxi"),  # the courier cancelled
    ],
)
def test_no_fee_is_counted_without_a_paid_cancel_yandex_shows(
    state: str, price: int | None, status: str
) -> None:
    shipment, claim = _paid_cancel_sent(price, state)
    assert claim.cancel_price == (price if state == "paid" else None)
    _record(shipment, _snapshot(claim, status))
    claim.refresh_from_db()
    assert claim.cancel_fee is None
    assert services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE) is None
    shipment.refresh_from_db()
    assert shipment.carrier_cost_total == 0


def test_a_paid_cancel_yandex_showed_before_it_was_marked_is_counted_by_txn_b() -> None:
    shipment = _accepted(status="pickup_arrived", stage=G.COURIER_ARRIVED)
    claim = shipment.claim
    assert claim is not None
    _record(shipment, _snapshot(claim, "cancelled_with_payment"))  # a poll, before our cancel was marked
    assert services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE) is None  # no price
    assert services.record_cancel_sent(
        shipment.order_id, claim.pk, cancel_state="paid", price=9_000, at=timeutils.now()
    )
    assert services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE) == 9_000
    assert services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE) == 9_000  # once
    shipment.refresh_from_db()
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (9_000, PAID_FEE - 9_000)
    with pytest.raises(ValueError):
        services.count_cancel_fee(shipment.order_id, claim.pk, paid_fee=PAID_FEE, quoted=-1)
    # a cancel refused while the claim got cancelled all the same counts the price this attempt read
    other = _accepted(status="pickup_arrived", stage=G.COURIER_ARRIVED)
    refused = other.claim
    assert refused is not None
    _record(other, _snapshot(refused, "cancelled_with_payment"))
    assert services.count_cancel_fee(other.order_id, refused.pk, paid_fee=PAID_FEE, quoted=7_000) == 7_000
    other.refresh_from_db()
    assert other.carrier_cost_total == 7_000


def _re_dispatched(*, confirmed: bool) -> tuple[Shipment, CourierClaim, CourierClaim]:
    """Claim 1 carried the parcel and brought it back (``returned_finish``; confirmed by staff or not); claim
    2, a re-dispatch Yandex found no courier for, is the current one and never took anything."""
    shipment = _accepted(status="pickuped", stage=G.IN_TRANSIT)
    first = shipment.claim
    assert first is not None
    _record(shipment, _snapshot(first, "returning"))
    first.refresh_from_db()
    _record(shipment, _snapshot(first, "returned", minutes=2))
    if confirmed:
        staff = StaffUserFactory.create()
        services.confirm_return(
            shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=True
        )
    first.refresh_from_db()
    _record(shipment, _snapshot(first, "returned_finish", minutes=3))
    second = CourierClaimFactory.create(
        shipment=shipment,
        status="performer_not_found",
        accept_sent_at=timeutils.now(),
        accepted_at=timeutils.now(),
        ended_at=timeutils.now(),
    )
    Shipment.objects.filter(pk=shipment.pk).update(claim=second, stage=G.RETURNED)
    shipment.refresh_from_db()
    return shipment, first, second


def test_the_return_belongs_to_the_claim_that_carried_the_parcel() -> None:
    shipment, first, second = _re_dispatched(confirmed=False)
    carried = selectors.return_claim(shipment.pk)
    assert carried is not None
    assert (carried.id, carried.number, carried.claim_id, carried.confirmed) == (
        first.pk,
        1,
        first.claim_id,
        False,
    )
    view = selectors.yandex_cancel_view(shipment.order_id)
    assert view is not None and view.claim is not None and view.claim.id == second.pk  # the current claim
    assert view.return_claim == carried and view.return_confirmed is False
    returned_at = shipment.returned_at
    staff = StaffUserFactory.create()
    assert services.confirm_return(
        shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=False
    )
    event = ShipmentEvent.objects.get(shipment=shipment, kind=ShipmentEvent.Kind.RETURNED)
    assert event.payload["claim_number"] == 1 and event.carrier_status == "returned_finish"
    shipment.refresh_from_db()
    assert shipment.returned_at == returned_at  # the claim that brought it back is not stale
    carried = selectors.return_claim(shipment.pk)
    assert carried is not None and carried.confirmed
    assert not services.confirm_return(
        shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=False
    )


def test_a_confirmed_return_is_not_offered_again_after_a_failed_re_dispatch() -> None:
    shipment, first, _second = _re_dispatched(confirmed=True)
    carried = selectors.return_claim(shipment.pk)
    assert carried is not None and carried.id == first.pk and carried.confirmed
    view = selectors.yandex_cancel_view(shipment.order_id)
    assert view is not None and view.return_confirmed is True
    staff = StaffUserFactory.create()
    assert not services.confirm_return(
        shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=False
    )
    assert ShipmentEvent.objects.filter(shipment=shipment, kind=ShipmentEvent.Kind.RETURNED).count() == 1
    # a parcel nobody ever took has no return to confirm
    untouched = _accepted()
    assert selectors.return_claim(untouched.pk) is None


def test_staff_marking_an_unaccounted_parcel_delivered_moves_only_the_stage() -> None:
    shipment = _accepted(status="pickuped", stage=G.IN_TRANSIT)
    claim = shipment.claim
    assert claim is not None
    _record(shipment, _snapshot(claim, "cancelled_with_items_on_hands"))
    shipment.refresh_from_db()
    before = (shipment.state, shipment.carrier_cost_total, shipment.cost_difference)
    assert shipment.stage == G.CANCELLED
    staff = StaffUserFactory.create()
    assert services.mark_delivered_by_staff(shipment.order_id, actor_id=staff.pk, at=timeutils.now()) is True
    shipment.refresh_from_db()
    assert shipment.stage == G.DELIVERED
    assert (shipment.state, shipment.carrier_cost_total, shipment.cost_difference) == before
    event = ShipmentEvent.objects.get(shipment=shipment, kind=ShipmentEvent.Kind.STATUS_RECEIVED)
    assert event.manual and event.payload["confirmed_by"] == staff.pk and event.bts_status == ""
    assert services.mark_delivered_by_staff(shipment.order_id, actor_id=staff.pk, at=timeutils.now()) is False
    bts = ShipmentFactory.create(created=True)
    assert services.mark_delivered_by_staff(bts.order_id, actor_id=staff.pk, at=timeutils.now()) is False


def test_a_confirmed_return_stays_returned_whatever_lags_behind() -> None:
    shipment = _accepted(status="returning", stage=G.RETURNING)
    claim = shipment.claim
    assert claim is not None
    staff = StaffUserFactory.create()
    services.request_carrier_cancel(shipment.order_id, at=timeutils.now())
    assert not selectors.return_confirmed(shipment.order_id, claim.number)
    confirmed = services.confirm_return(
        shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=True
    )
    assert confirmed is True
    shipment.refresh_from_db()
    assert (shipment.stage, shipment.return_confirmed_by_id) == (G.RETURNED, staff.pk)
    assert shipment.returned_at is not None and shipment.cancel_requested_at is None
    event = ShipmentEvent.objects.get(shipment=shipment, kind=ShipmentEvent.Kind.RETURNED)
    assert event.payload == {
        "claim_id": claim.claim_id,
        "claim_number": claim.number,
        "confirmed_by": staff.pk,
    }
    assert selectors.return_confirmed(shipment.order_id, claim.number)
    assert (
        services.confirm_return(
            shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=True
        )
        is False
    )
    update = _record(shipment, _snapshot(claim, "ready_for_return_confirmation"))
    assert update.applied and update.stage == G.RETURNED
    shipment.refresh_from_db()
    assert shipment.stage == G.RETURNED


def test_confirm_return_needs_a_returning_parcel() -> None:
    shipment = _accepted()
    staff = StaffUserFactory.create()
    assert (
        services.confirm_return(
            shipment.order_id, actor_id=staff.pk, at=timeutils.now(), clear_cancel_request=False
        )
        is False
    )
    assert not ShipmentEvent.objects.filter(kind=ShipmentEvent.Kind.RETURNED).exists()


@pytest.mark.parametrize(
    ("status", "stamped", "expected"),
    [
        ("cancelled", True, True),
        ("cancelled_with_payment", True, True),
        ("cancelled", False, False),  # Yandex cancelled without us: not our carrier cancel
        ("cancelled_by_taxi", True, False),  # the courier cancelled
        ("failed", True, False),
    ],
)
def test_the_shipment_of_a_claim_we_cancelled_is_cancelled(
    status: str, stamped: bool, expected: bool
) -> None:
    shipment = _accepted()
    claim = shipment.claim
    assert claim is not None
    if stamped:
        services.request_carrier_cancel(shipment.order_id, at=timeutils.now())
    _record(shipment, _snapshot(claim, status))
    assert services.mark_yandex_cancelled(shipment.order_id, at=timeutils.now()) is expected
    shipment.refresh_from_db()
    assert (shipment.state == Shipment.State.CANCELLED) is expected
    assert (shipment.cancelled_at is not None) is expected


def test_the_cancel_view_and_the_review_selectors() -> None:
    shipment = _accepted(status="pickuped", stage=G.IN_TRANSIT)
    claim = shipment.claim
    assert claim is not None
    view = selectors.yandex_cancel_view(shipment.order_id)
    assert view is not None
    assert (view.stage, view.claim_open, view.picked_up, view.returning) == (G.IN_TRANSIT, True, True, False)
    assert view.claim is not None and view.claim.destination_point_id is None
    _record(shipment, _snapshot(claim, "delivery_arrived"))
    claim.refresh_from_db()
    _record(shipment, _snapshot(claim, "failed", minutes=2))  # gone after pickup: the history knows it
    view = selectors.yandex_cancel_view(shipment.order_id)
    assert view is not None and view.picked_up and not view.claim_open
    assert selectors.yandex_cancel_view(yandex_shipment().order_id) is not None
    searching = _accepted(status="performer_lookup", stage=G.COURIER_REQUESTED)
    accepted_at = timeutils.now() - timedelta(minutes=31)
    CourierClaim.objects.filter(shipment=searching).update(accepted_at=accepted_at)
    assert selectors.slow_courier_searches(timeutils.now() - timedelta(minutes=30)) == [
        (searching.order_id, 1)
    ]
    assert selectors.slow_courier_searches(timeutils.now() - timedelta(minutes=40)) == []
    assert searching.order_id in selectors.yandex_orders_at({G.COURIER_REQUESTED})
    services.request_carrier_cancel(searching.order_id, at=timeutils.now() - timedelta(minutes=11))
    (request,) = selectors.cancel_requests(timeutils.now() - timedelta(minutes=10))
    assert (request.order_id, request.claim_status, request.claim_ended) == (
        searching.order_id,
        "performer_lookup",
        False,
    )
