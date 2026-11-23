"""The legacy ``push.session_replaced`` handler and its dead-letter hook: since B-W1 nothing produces the
topic, they only drain rows enqueued before the release."""

from __future__ import annotations

import logging
from typing import Any

from glowderma.accounts import services, topics
from glowderma.accounts.models import Device
from glowderma.core import enums, outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.fcm.ports import PAYLOAD_INVALID, UNREGISTERED, PushMessage
from glowderma.integrations.fcm.registry import get_fcm_port

logger = logging.getLogger("glowderma.accounts.tasks")


@outbox.register(topics.PUSH_SESSION_REPLACED)
def push_session_replaced(message: OutboxMessage) -> None:
    """Data-only ``{"type": "session_replaced", "sid": …}`` to the replaced device (customer contract §1.9
    step 4).

    No notification text and no PII; the app acts only when ``sid`` is the session it holds. After a
    definitive answer the token is blanked in the stored payload, so a redelivery sends nothing and no token
    stays behind. ``unregistered`` prunes every device still holding the token; ``payload_invalid`` logs an
    ERROR and ends the message (never touching a device); ``FcmUnavailable`` propagates so the outbox retries.
    """
    payload: dict[str, Any] = dict(message.payload) if isinstance(message.payload, dict) else {}
    token = str(payload.get("fcm_token") or "")
    if not token:
        return
    data = {"type": enums.PushDataType.SESSION_REPLACED.value, "sid": str(payload.get("sid", ""))}
    [result] = get_fcm_port().send([PushMessage.data_message(token, data)])
    if not result.ok and result.error == UNREGISTERED:
        for device_id in Device.objects.filter(fcm_token=token).values_list("pk", flat=True):
            services.invalidate_fcm_token(device_id, token=token)
    elif not result.ok and result.error == PAYLOAD_INVALID:
        logger.error("push.payload_invalid topic=%s field=data", message.topic)
    elif not result.ok:
        logger.warning("push.session_replaced failed error=%s outbox_id=%s", result.error, message.pk)
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, "fcm_token": ""}, updated_at=timeutils.now()
    )


@outbox.on_dead(topics.PUSH_SESSION_REPLACED)
def blank_dead_session_replaced_token(message: OutboxMessage) -> None:
    """A ``push.session_replaced`` that died (FCM unavailable until the last attempt) keeps no token either
    (customer contract §1.9 step 4: the token does not stay behind in the outbox row)."""
    payload: dict[str, Any] = dict(message.payload) if isinstance(message.payload, dict) else {}
    if payload.get("fcm_token"):
        OutboxMessage.objects.filter(pk=message.pk).update(
            payload={**payload, "fcm_token": ""}, updated_at=timeutils.now()
        )


__all__ = [
    "push_session_replaced",
    "blank_dead_session_replaced_token",
]
