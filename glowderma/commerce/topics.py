"""Outbox topics ``commerce`` produces or handles (design 03 §5; plan §4.2) — constants only.

Handlers arrive with B5 in ``commerce/tasks.py`` (``@outbox.register(topic)``, at-least-once, idempotent at
the provider boundary) and the dead-letter hooks through ``core.outbox.on_dead``. ``enqueue`` is
``get_or_create(dedupe_key)``, so the dedupe-key builders below are the idempotency contract between producer
and handler. Topics of other modules (``push.appointment``, ``campaign.send_batch``, ``sms.send``) live with
their producers; ``bts.cancel_shipment`` is a synchronous call inside ``cancel_order``, never a topic.
"""

from __future__ import annotations

# ``bts.create_shipment`` / ``refund.submit`` are enqueued by the lower modules (``delivery``, ``payments``),
# which cannot import this one: their names and dedupe-key builders live there and are re-exported here.
from glowderma.delivery.topics import BTS_CREATE_SHIPMENT, bts_create_shipment_key
from glowderma.payments.topics import REFUND_SUBMIT, refund_submit_key

BILLZ_PUSH_SALE = "billz.push_sale"
PUSH_ORDER_STATUS = "push.order_status"
# Gap X-STOCK-FRESHNESS: after a pushed sale, read the sold products' stock from Billz again (best effort).
BILLZ_STOCK_REREAD = "billz.stock_reread"

# Topics whose handler is ``commerce/tasks.py`` (B5).
HANDLED_TOPICS: tuple[str, ...] = (BTS_CREATE_SHIPMENT, BILLZ_PUSH_SALE, REFUND_SUBMIT, BILLZ_STOCK_REREAD)
# Topics ``commerce.services`` enqueues directly (``bts.create_shipment`` goes through
# ``delivery.services.request_bts_create``; ``refund.submit`` through ``payments.services.create_refund``);
# ``billz.stock_reread`` is enqueued by the ``billz.push_sale`` handler.
PRODUCED_TOPICS: tuple[str, ...] = (BILLZ_PUSH_SALE, PUSH_ORDER_STATUS, BILLZ_STOCK_REREAD)
# Topics whose ``dead`` message has a business consequence recorded by ``commerce.tasks.on_outbox_dead``:
# the shipment / refund is marked failed with ``dead_letter`` and a ``shipment_failed`` / ``refund_failed``
# event is written (design 03 §5; review major 5: never ``unknown``).
DEAD_LETTER_TOPICS: tuple[str, ...] = (BTS_CREATE_SHIPMENT, REFUND_SUBMIT)
DEAD_LETTER_ERROR_CODE = "dead_letter"

# Payload keys per topic (JSON objects; every key always present).
PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    BTS_CREATE_SHIPMENT: ("order_id", "manual", "request_no", "actor_id"),
    BILLZ_PUSH_SALE: ("purchase_id", "order_id", "billz_order_uuid"),
    REFUND_SUBMIT: ("refund_id", "payment_id", "order_id", "provider_txn_id", "amount"),
    PUSH_ORDER_STATUS: ("order_id", "status", "event_id", "customer_id"),
    BILLZ_STOCK_REREAD: ("order_id", "product_ids"),
}


def billz_push_sale_key(billz_order_uuid: str) -> str:
    return f"{BILLZ_PUSH_SALE}:{billz_order_uuid}"


def billz_stock_reread_key(order_id: int) -> str:
    """One re-read per pushed sale (the push runs once; a redelivered success enqueues nothing new)."""
    return f"{BILLZ_STOCK_REREAD}:{order_id}"


def push_order_status_key(order_id: int, status: str) -> str:
    """One push per order per status, whatever the number of deliveries or producers."""
    return f"{PUSH_ORDER_STATUS}:{order_id}:{status}"


def order_status_inbox_key(order_id: int, status: str) -> str:
    """``InboxNotification.dedupe_key`` the notifications handler claims for ``push.order_status``."""
    return f"order_status:{order_id}:{status}"


__all__ = [
    "BILLZ_PUSH_SALE",
    "BILLZ_STOCK_REREAD",
    "BTS_CREATE_SHIPMENT",
    "DEAD_LETTER_ERROR_CODE",
    "DEAD_LETTER_TOPICS",
    "HANDLED_TOPICS",
    "PAYLOAD_KEYS",
    "PRODUCED_TOPICS",
    "PUSH_ORDER_STATUS",
    "REFUND_SUBMIT",
    "billz_push_sale_key",
    "billz_stock_reread_key",
    "bts_create_shipment_key",
    "order_status_inbox_key",
    "push_order_status_key",
    "refund_submit_key",
]
