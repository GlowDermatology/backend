"""Outbox topics ``notifications`` handles (design 03 §5): the two status pushes produced below it, its own
``campaign.send_batch`` and the M2 ``push.account_security`` notices produced by ``accounts`` (customer
contract §2.14.2). Dedupe keys are the idempotency contract between producer and handler; the
``InboxNotification.dedupe_key`` of every delivery mirrors them (one inbox row per customer per message).
"""

from __future__ import annotations

from glowderma.accounts.topics import ACCOUNT_SECURITY_PAYLOAD_KEYS, PUSH_ACCOUNT_SECURITY
from glowderma.appointments.topics import PUSH_APPOINTMENT
from glowderma.appointments.topics import inbox_key as appointment_inbox_key
from glowderma.commerce.topics import PUSH_ORDER_STATUS, order_status_inbox_key

CAMPAIGN_SEND_BATCH = "campaign.send_batch"
HANDLED_TOPICS: tuple[str, ...] = (
    PUSH_ORDER_STATUS,
    PUSH_APPOINTMENT,
    CAMPAIGN_SEND_BATCH,
    PUSH_ACCOUNT_SECURITY,
)
# Payload keys per topic (JSON objects; every key always present).
PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    PUSH_ORDER_STATUS: ("order_id", "status", "event_id", "customer_id"),
    PUSH_APPOINTMENT: ("appointment_id", "action", "event_id", "customer_id", "reschedule_no"),
    CAMPAIGN_SEND_BATCH: ("campaign_id", "batch_no"),
    # the one definition (accounts.topics, customer contract §2.14.2), imported, never re-typed
    PUSH_ACCOUNT_SECURITY: ACCOUNT_SECURITY_PAYLOAD_KEYS,
}

__all__ = [
    "CAMPAIGN_SEND_BATCH",
    "HANDLED_TOPICS",
    "PAYLOAD_KEYS",
    "PUSH_ACCOUNT_SECURITY",
    "PUSH_APPOINTMENT",
    "PUSH_ORDER_STATUS",
    "appointment_inbox_key",
    "campaign_inbox_key",
    "campaign_send_batch_key",
    "order_status_inbox_key",
]


def campaign_send_batch_key(campaign_id: int, batch_no: int) -> str:
    return f"{CAMPAIGN_SEND_BATCH}:{campaign_id}:{batch_no}"


def campaign_inbox_key(campaign_id: int, customer_id: int) -> str:
    """``InboxNotification.dedupe_key`` of a campaign delivery: one per customer per campaign."""
    return f"campaign:{campaign_id}:{customer_id}"
