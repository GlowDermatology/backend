"""Outbox handlers and the beat task of `notifications` (design 03 §3.0 / §5).

Imported at startup by ``NotificationsConfig.ready`` so the ``@outbox.register`` handlers exist in every
process that may run ``core.dispatch_outbox``. Each handler is idempotent at the provider boundary
(``InboxNotification.dedupe_key`` claims, ``CampaignRecipient`` states); a message that dies after ten
attempts is audited as ``notification.push_dead`` by the ``on_dead`` hook.
"""

from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from glowderma.core import audit, outbox
from glowderma.core.models import OutboxMessage
from glowderma.notifications import services, topics


@outbox.register(topics.PUSH_ORDER_STATUS)
def handle_push_order_status(message: OutboxMessage) -> None:
    services.deliver_order_status(message.payload)


@outbox.register(topics.PUSH_APPOINTMENT)
def handle_push_appointment(message: OutboxMessage) -> None:
    services.deliver_appointment(message.payload)


@outbox.register(topics.CAMPAIGN_SEND_BATCH)
def handle_campaign_send_batch(message: OutboxMessage) -> None:
    services.send_batch(int(message.payload["campaign_id"]), int(message.payload["batch_no"]))


def on_push_dead(message: OutboxMessage) -> None:
    """A notification that could not be delivered in ten attempts: record it for the operators."""
    payload: dict[str, Any] = message.payload if isinstance(message.payload, dict) else {}
    audit.record(
        action="notification.push_dead",
        entity_type="outbox",
        entity_id=str(message.pk),
        after={
            "topic": message.topic,
            "dedupe_key": message.dedupe_key,
            "attempts": message.attempts,
            "last_error": message.last_error,
            "customer_id": payload.get("customer_id"),
            "campaign_id": payload.get("campaign_id"),
        },
    )


for _topic in topics.HANDLED_TOPICS:
    outbox.on_dead(_topic)(on_push_dead)


@shared_task(name="notifications.start_due_campaigns")  # type: ignore[untyped-decorator]
def start_due_campaigns() -> list[int]:
    return services.start_due_campaigns()
