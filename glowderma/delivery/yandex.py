"""Yandex Delivery claim statuses and what they mean here (plan §3.3, §3.7). Pure: not an adapter.

The 26 literals are the ``ClaimStatus`` enum of the Yandex Delivery API (docs rev. r22073422), used verbatim;
``pay_waiting`` appears in the status table but not in that enum and, like any status not listed here, maps
to nothing (:func:`stage_for` returns ``None``: the caller stores and logs it, nothing moves).

Terminal statuses: **no documentation page lists them.** :data:`FINISHED` is our reading of the wording
("The order is completed", "completed with a return", the cancellations, "Further execution is impossible",
"Try creating a new claim later"). Two of them are judgement calls: ``estimating_failed`` is documented as
recoverable by editing the claim — we never edit, so for us that claim is over — and whether
``cancelled_by_taxi`` makes Yandex search for another courier on the same claim is an open question for the
account manager (docs/decisions/open-questions.md); until it is answered the claim counts as finished.
"""

from __future__ import annotations

from glowderma.core import enums

_G = enums.ShipmentStage
_S = enums.OrderStatus

# Every documented ``ClaimStatus``, in the order of the docs' status table.
STATUSES: tuple[str, ...] = (
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
)

# Claim status → the shipment stage it shows. A courier that cancels before pickup (``cancelled_by_taxi``)
# leaves the order where ``performer_not_found`` does: no courier, staff dispatch again.
STAGE_BY_STATUS: dict[str, str] = {
    "new": _G.COURIER_REQUESTED,
    "estimating": _G.COURIER_REQUESTED,
    "ready_for_approval": _G.COURIER_REQUESTED,
    "accepted": _G.COURIER_REQUESTED,
    "performer_lookup": _G.COURIER_REQUESTED,
    "performer_draft": _G.COURIER_REQUESTED,
    "performer_found": _G.COURIER_ASSIGNED,
    "pickup_arrived": _G.COURIER_ARRIVED,
    "ready_for_pickup_confirmation": _G.COURIER_ARRIVED,
    "pickuped": _G.IN_TRANSIT,
    "delivery_arrived": _G.AT_DESTINATION,
    "ready_for_delivery_confirmation": _G.AT_DESTINATION,
    "delivered": _G.DELIVERED,
    "delivered_finish": _G.DELIVERED,
    "returning": _G.RETURNING,
    "return_arrived": _G.RETURNING,
    "ready_for_return_confirmation": _G.RETURNING,
    "returned": _G.RETURNED,
    "returned_finish": _G.RETURNED,
    "cancelled": _G.CANCELLED,
    "cancelled_with_payment": _G.CANCELLED,
    "cancelled_by_taxi": _G.FAILED,
    "cancelled_with_items_on_hands": _G.CANCELLED,
    "failed": _G.FAILED,
    "estimating_failed": _G.FAILED,
    "performer_not_found": _G.FAILED,
}

# Claim status → the order status it moves an open order to (state.py rows 11–12, trigger yandex_status).
ORDER_EFFECT: dict[str, str] = {
    "pickuped": _S.HANDED_TO_COURIER,
    "delivered": _S.DELIVERED,
    "delivered_finish": _S.DELIVERED,
}

# Statuses reached only after the courier took the parcel (a poll may skip ``pickuped`` itself).
PICKED_UP: frozenset[str] = frozenset(
    {
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
        "cancelled_with_items_on_hands",
    }
)

# Statuses after which the claim does nothing more (see the module docstring: our reading, not a doc list).
FINISHED: frozenset[str] = frozenset(
    {
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
)

# Stages at which an order can be cancelled without a carrier call although a claim may still be open: the
# parcel is on its way back or back at the warehouse (plan §3.8 "returning / returned").
RETURN_STAGES: frozenset[str] = frozenset({_G.RETURNING, _G.RETURNED})

# Before ``claims/accept`` took effect: no courier is searched for, the claim summons nobody (plan §3.6).
PRE_ACCEPT: frozenset[str] = frozenset({"new", "estimating", "ready_for_approval"})
# Statuses Yandex reaches only after the claim was accepted (a courier was searched for). ``failed`` and
# ``cancelled`` are not among them: an unaccepted claim can end in either (an expired offer, our own cancel).
ACCEPT_IMPLIED: frozenset[str] = (
    frozenset(STATUSES) - PRE_ACCEPT - {"estimating_failed", "failed", "cancelled"}
)
DELIVERED_STATUSES: frozenset[str] = frozenset({"delivered", "delivered_finish"})
RETURNED_STATUSES: frozenset[str] = frozenset({"returned", "returned_finish"})
CANCELLED_STATUSES: frozenset[str] = frozenset(
    {"cancelled", "cancelled_with_payment", "cancelled_by_taxi", "cancelled_with_items_on_hands"}
)
# A claim that ends here never delivered nor returned anything: no courier came (dispatch again) or the
# parcel is out of our sight (after pickup).
FAILURE_STATUSES: frozenset[str] = CANCELLED_STATUSES | {"failed", "estimating_failed", "performer_not_found"}
# Finished claims Yandex charges nothing for, by our reading of the docs (no courier took the parcel or none
# came; the documented fees are for a cancellation after the courier's arrival and for deliveries/returns):
# settled at once with no cost instead of waiting 24 h for a ``final_price``.
NO_COST_ENDS: frozenset[str] = frozenset(
    {"estimating_failed", "performer_not_found", "cancelled", "cancelled_by_taxi"}
)
# Yandex accepted the claim and is still searching for a courier (no performer yet): the review beat flags a
# search that takes longer than 30 minutes (``dispatch_failed`` ``courier_search_slow``).
SEARCHING: frozenset[str] = frozenset({"accepted", "performer_lookup", "performer_draft"})
# Stages with a courier on the claim (the tracking link exists from here, the courier's name and car too).
COURIER_STAGES: frozenset[str] = frozenset(
    {_G.COURIER_ASSIGNED, _G.COURIER_ARRIVED, _G.IN_TRANSIT, _G.AT_DESTINATION}
)


def stage_for(status: str) -> str | None:
    """The :class:`~glowderma.core.enums.ShipmentStage` of a claim status; ``None`` for an unknown one."""
    return STAGE_BY_STATUS.get(status)


def order_effect(status: str) -> str | None:
    """The order status a claim status leads to (``None`` = the order does not move)."""
    return ORDER_EFFECT.get(status)


def is_finished(status: str) -> bool:
    return status in FINISHED


def is_picked_up(status: str) -> bool:
    return status in PICKED_UP


def cancel_without_carrier(stage: str, *, claim_open: bool) -> bool:
    """An order with this Yandex shipment can be cancelled without calling Yandex: no claim is open (nothing
    was summoned, or the last claim is finished), or the parcel is returning / back at the warehouse."""
    return not claim_open or stage in RETURN_STAGES


__all__ = [
    "ACCEPT_IMPLIED",
    "CANCELLED_STATUSES",
    "COURIER_STAGES",
    "DELIVERED_STATUSES",
    "FAILURE_STATUSES",
    "FINISHED",
    "NO_COST_ENDS",
    "ORDER_EFFECT",
    "PICKED_UP",
    "PRE_ACCEPT",
    "RETURNED_STATUSES",
    "RETURN_STAGES",
    "SEARCHING",
    "STAGE_BY_STATUS",
    "STATUSES",
    "cancel_without_carrier",
    "is_finished",
    "is_picked_up",
    "order_effect",
    "stage_for",
]
