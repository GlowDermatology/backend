"""BTS status codes and what they mean for the order (design 02 §3.1).

BTS reports a numeric status per parcel (webhook or polling). Only three codes drive the order in M1:
``100`` accepted (the parcel is at the BTS warehouse: the only status a cancel succeeds at, O1), ``200``
handed to the courier, ``300`` delivered. Every other code is recorded on the shipment (``ShipmentEvent``)
and changes nothing on the order.
"""

from __future__ import annotations

from glowderma.core import enums

ACCEPTED = "100"
HANDED_TO_COURIER = "200"
DELIVERED = "300"

# BTS status code → the order status it moves a processing / handed order to (None = no order change).
STATUS_MAP: dict[str, str | None] = {
    ACCEPTED: None,
    HANDED_TO_COURIER: enums.OrderStatus.HANDED_TO_COURIER,
    DELIVERED: enums.OrderStatus.DELIVERED,
}


def map_status(code: str) -> str | None:
    """The ``OrderStatus`` a BTS status code leads to, or ``None`` when it does not move the order."""
    return STATUS_MAP.get(str(code))


def is_cancellable(code: str) -> bool:
    """BTS cancels a parcel only while it reports ``100`` (or before any status arrived)."""
    return str(code) in ("", ACCEPTED)
