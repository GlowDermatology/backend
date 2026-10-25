"""Read side of `delivery` (design 02 §3.1 API rendering, derived ``pending``; 03 §6.4 ``bts_failed``).

The contract's ``shipment`` union (``OrderSummary.shipment``):

| state | keys |
|---|---|
| ``not_required`` | — (pickup: no row) |
| ``not_created`` | ``pending`` |
| ``failed`` | ``last_attempt_at``, ``attempts``, ``pending`` |
| ``created`` | ``tracking_number``, ``tariff``, ``updated_at``, ``via`` (also a ``cancelled`` parcel, D-1) |

``pending`` is true while the newest ``bts.create_shipment`` message is ``pending``/``failed``/``in_flight``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from glowderma.core import enums
from glowderma.delivery.models import Shipment


@dataclass(frozen=True, slots=True)
class ShipmentInfo:
    """The internal state of one order's shipment (what ``commerce`` checks under the Order lock)."""

    order_id: int
    state: str  # internal Shipment.State (not_created|failed|created|cancelled)
    pending: bool
    tracking_number: str
    tariff: int | None
    bts_order_id: str
    bts_status: str
    attempts: int
    create_requests: int


def shipment_info(order_id: int) -> ShipmentInfo | None:
    """The order's shipment (``None`` for pickup orders and orders without a row)."""
    row = Shipment.objects.with_pending().filter(order_id=order_id).first()
    if row is None:
        return None
    return ShipmentInfo(
        order_id=row.order_id,
        state=row.state,
        pending=bool(row.pending),  # type: ignore[attr-defined]
        tracking_number=row.tracking_number,
        tariff=row.tariff,
        bts_order_id=row.bts_order_id,
        bts_status=row.bts_status,
        attempts=row.attempts,
        create_requests=row.create_requests,
    )


def api_state(shipment: Shipment | None, fulfilment: str) -> str:
    """The contract ``ShipmentState`` of an order (``cancelled`` renders as ``created``, D-1)."""
    if fulfilment == enums.FulfilmentMethod.PICKUP:
        return enums.ShipmentState.NOT_REQUIRED
    if shipment is None or shipment.state == Shipment.State.NOT_CREATED:
        return enums.ShipmentState.NOT_CREATED
    if shipment.state == Shipment.State.FAILED:
        return enums.ShipmentState.FAILED
    return enums.ShipmentState.CREATED


def updated_at(shipment: Shipment) -> datetime | None:
    """``COALESCE(last_status_at, bts_created_at)`` — the created state's ``updated_at``."""
    return shipment.last_status_at or shipment.bts_created_at


def shipment_view(
    shipment: Shipment | None, fulfilment: str, *, pending: bool | None = None
) -> dict[str, Any]:
    """The contract union of one order; ``shipment`` loaded with ``create_message`` (or ``pending`` given)."""
    state = api_state(shipment, fulfilment)
    if state == enums.ShipmentState.NOT_REQUIRED:
        return {"state": state}
    if pending is None:
        pending = shipment.is_pending if shipment is not None else False
    if state == enums.ShipmentState.NOT_CREATED:
        return {"state": state, "pending": pending}
    if shipment is None:  # unreachable: api_state returned failed/created
        raise ValueError("shipment row expected")
    if state == enums.ShipmentState.FAILED:
        return {
            "state": state,
            "last_attempt_at": shipment.last_attempt_at,
            "attempts": shipment.attempts,
            "pending": pending,
        }
    return {
        "state": state,
        "tracking_number": shipment.tracking_number,
        "tariff": shipment.tariff,
        "updated_at": updated_at(shipment),
        "via": shipment.via or enums.ShipmentVia.POLLING,
    }


def shipment_for_order(order_id: int, fulfilment: str) -> dict[str, Any]:
    """The contract union of one order (one query)."""
    row = Shipment.objects.select_related("create_message").filter(order_id=order_id).first()
    return shipment_view(row, fulfilment)


def bts_failed_count() -> int:
    """Dashboard ``attention.bts_failed``: processing orders whose shipment is ``failed`` (02 §3.1)."""
    return Shipment.objects.filter(
        state=Shipment.State.FAILED, order__status=enums.OrderStatus.PROCESSING
    ).count()


__all__ = [
    "ShipmentInfo",
    "api_state",
    "bts_failed_count",
    "shipment_for_order",
    "shipment_info",
    "shipment_view",
    "updated_at",
]
