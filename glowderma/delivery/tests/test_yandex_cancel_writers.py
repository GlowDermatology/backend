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
from glowderma.delivery.tests.factories import CourierClaimFactory, yandex_shipment
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
    services.request_carrier_cancel(shipment.order_id, at=timeutils.now())
    shipment.refresh_from_db()
    assert shipment.cancel_requested_at is not None
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
