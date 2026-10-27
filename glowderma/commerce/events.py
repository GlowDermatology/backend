"""The typed ``data`` of every ``OrderEvent`` type (contract §4; design 03 §1.5).

``commerce.services.record_event`` validates the key set of each event against :data:`EVENT_DATA_KEYS`
before inserting, so a missing or extra key fails the writing transaction instead of the reader.
"""

from __future__ import annotations

from glowderma.core import enums

_E = enums.OrderEventType

EVENT_DATA_KEYS: dict[str, tuple[str, ...]] = {
    _E.ORDER_CREATED: ("channel", "item_count", "total"),
    _E.RESERVATION_CREATED: ("item_count", "minutes", "expires_at"),
    _E.PAYMENT_CONFIRMED: ("provider", "transaction_id", "amount", "reservation_converted"),
    _E.BILLZ_SALE_RECORDED: ("billz_order_id",),
    _E.CASHBACK_CREDITED: ("ledger_entry_id", "amount", "base", "percent", "policy_version"),
    _E.PAYMENT_FAILED: ("provider", "transaction_id", "error_code"),
    _E.SHIPMENT_CREATED: ("tracking_number", "tariff", "manual"),
    _E.SHIPMENT_FAILED: ("attempt", "manual", "error_code"),
    _E.STATUS_CHANGED: ("from", "to", "push_sent"),
    _E.HANDED_TO_COURIER: ("via", "bts_status", "push_sent"),
    _E.DELIVERED: ("via", "bts_status", "push_sent"),
    _E.CANCELLED: (
        "reason",
        "note",
        "refund_id",
        "cashback_reversed",
        "stock_released",
        "bts_cancelled",
        "push_sent",
    ),
    _E.RESERVATION_EXPIRED: ("released_item_count", "payment_failed"),
    # ``manual_reason`` (M2, admin §4): "payme" | "no_paydoc" | "fake_mode" when ``manual``, else null
    _E.REFUND_REQUESTED: ("refund_id", "amount", "provider", "manual", "manual_reason"),
    _E.REFUND_SUCCEEDED: ("refund_id", "amount", "provider"),
    _E.REFUND_FAILED: ("refund_id", "amount", "provider", "state", "error_code"),
}

# Events written by at-least-once handlers carry a discriminating key: a second delivery finds the event
# (same type, same value) and writes nothing (03 §1.5 invariants).
DEDUPE_KEYS: dict[str, tuple[str, ...]] = {
    _E.BILLZ_SALE_RECORDED: ("billz_order_id",),
    _E.SHIPMENT_CREATED: ("tracking_number",),
    _E.SHIPMENT_FAILED: ("attempt",),
    _E.REFUND_REQUESTED: ("refund_id",),
    _E.REFUND_SUCCEEDED: ("refund_id",),
    _E.REFUND_FAILED: ("refund_id", "state"),  # an ``unknown`` refund resolved as ``failed`` is a second row
}


def check_data(event_type: str, data: dict[str, object]) -> None:
    """Raise ``ValueError`` unless ``data`` has exactly the contract keys of ``event_type``."""
    expected = EVENT_DATA_KEYS.get(event_type)
    if expected is None:
        raise ValueError(f"Unknown order event type {event_type!r}")
    if set(data) != set(expected):
        raise ValueError(f"{event_type} data keys {sorted(data)} != contract keys {sorted(expected)}")
