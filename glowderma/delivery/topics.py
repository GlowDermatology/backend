"""The outbox topic ``delivery`` produces (design 03 §5): ``bts.create_shipment``.

``delivery.services.request_bts_create`` enqueues it; the handler lives in ``commerce/tasks.py`` (the
orchestrator that needs the order and writes its events). ``commerce.topics`` re-exports these names so the
producer and the handler share one dedupe-key builder.
"""

from __future__ import annotations

BTS_CREATE_SHIPMENT = "bts.create_shipment"


def bts_create_shipment_key(order_id: int, request_no: int) -> str:
    """One message per enqueue: ``request_no`` is ``Shipment.create_requests`` after the increment."""
    return f"{BTS_CREATE_SHIPMENT}:{order_id}:{request_no}"
