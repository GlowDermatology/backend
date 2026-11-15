"""The Yandex claim status table (plan §3.3): every documented status has a stage, the order effects and the
finished set are exactly the documented reading, unknown statuses map to nothing."""

from glowderma.core import enums
from glowderma.delivery import yandex

# The ``ClaimStatus`` enum of the Yandex Delivery API, copied from the docs (rev. r22073422).
DOCUMENTED = [
    "new",
    "estimating",
    "ready_for_approval",
    "accepted",
    "performer_lookup",
    "performer_draft",
    "performer_found",
    "pickup_arrived",
    "ready_for_pickup_confirmation",
    "pickuped",
    "delivery_arrived",
    "ready_for_delivery_confirmation",
    "delivered",
    "delivered_finish",
    "returning",
    "return_arrived",
    "ready_for_return_confirmation",
    "returned",
    "returned_finish",
    "cancelled",
    "cancelled_with_payment",
    "cancelled_by_taxi",
    "cancelled_with_items_on_hands",
    "failed",
    "estimating_failed",
    "performer_not_found",
]


def test_every_documented_status_has_a_stage() -> None:
    assert len(DOCUMENTED) == 26
    assert list(yandex.STATUSES) == DOCUMENTED
    assert set(yandex.STAGE_BY_STATUS) == set(DOCUMENTED)
    assert set(yandex.STAGE_BY_STATUS.values()) <= set(enums.ShipmentStage.values)
    # every stage but the one before any claim is reachable from a status
    assert set(enums.ShipmentStage.values) - set(yandex.STAGE_BY_STATUS.values()) == {"awaiting_dispatch"}


def test_stages_of_the_main_flow() -> None:
    S = enums.ShipmentStage
    assert [yandex.stage_for(s) for s in ("new", "ready_for_approval", "accepted", "performer_draft")] == [
        S.COURIER_REQUESTED
    ] * 4
    assert yandex.stage_for("performer_found") == S.COURIER_ASSIGNED
    assert yandex.stage_for("pickup_arrived") == S.COURIER_ARRIVED
    assert yandex.stage_for("pickuped") == S.IN_TRANSIT
    assert yandex.stage_for("delivery_arrived") == S.AT_DESTINATION
    assert yandex.stage_for("delivered_finish") == S.DELIVERED
    assert yandex.stage_for("return_arrived") == S.RETURNING
    assert yandex.stage_for("returned_finish") == S.RETURNED
    assert yandex.stage_for("cancelled_with_payment") == S.CANCELLED
    assert yandex.stage_for("cancelled_by_taxi") == S.FAILED  # no courier any more: dispatch again
    assert yandex.stage_for("performer_not_found") == S.FAILED


def test_unknown_statuses_map_to_nothing() -> None:
    for status in ("pay_waiting", "", "CANCELLED", "canceled", "something_new"):
        assert yandex.stage_for(status) is None
        assert yandex.order_effect(status) is None
        assert not yandex.is_finished(status)
        assert not yandex.is_picked_up(status)


def test_order_effects() -> None:
    assert yandex.ORDER_EFFECT == {
        "pickuped": "handed_to_courier",
        "delivered": "delivered",
        "delivered_finish": "delivered",
    }
    assert yandex.order_effect("returning") is None and yandex.order_effect("accepted") is None


def test_finished_and_picked_up_sets() -> None:
    assert yandex.FINISHED == {
        "delivered_finish",
        "returned_finish",
        "cancelled",
        "cancelled_with_payment",
        "cancelled_by_taxi",
        "cancelled_with_items_on_hands",
        "failed",
        "estimating_failed",
        "performer_not_found",
    }
    assert yandex.FINISHED <= set(DOCUMENTED) and yandex.PICKED_UP <= set(DOCUMENTED)
    assert "pickuped" in yandex.PICKED_UP and "performer_found" not in yandex.PICKED_UP
    assert not {"delivered", "returned"} & yandex.FINISHED  # multi-point / not yet completed


def test_cancel_without_carrier() -> None:
    S = enums.ShipmentStage
    assert yandex.cancel_without_carrier(S.AWAITING_DISPATCH, claim_open=False)
    assert yandex.cancel_without_carrier("", claim_open=False)
    assert not yandex.cancel_without_carrier(S.COURIER_ASSIGNED, claim_open=True)
    assert not yandex.cancel_without_carrier(S.IN_TRANSIT, claim_open=True)
    assert yandex.cancel_without_carrier(S.RETURNING, claim_open=True)
    assert yandex.cancel_without_carrier(S.RETURNED, claim_open=True)
