"""Background work of ``accounts``: the ``push.session_replaced`` handler and the ``accounts.cleanup_auth``
job.

P1 appends the ``sms.send`` and ``apple.revoke`` handlers (and the ``apple.revoke`` dead-letter hook) below.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.db.models import Q

from glowderma.accounts import services, topics
from glowderma.accounts.models import AuthSession, Device, SmsCode
from glowderma.core import enums, outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.fcm.ports import PAYLOAD_INVALID, UNREGISTERED, PushMessage
from glowderma.integrations.fcm.registry import get_fcm_port

logger = logging.getLogger(__name__)

# accounts.cleanup_auth retention (customer contract §6.4, plan §8).
SMS_RETENTION = timedelta(hours=24)
SESSION_RETENTION = timedelta(days=90)


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


@shared_task(name="accounts.cleanup_auth")  # type: ignore[untyped-decorator]
def cleanup_auth() -> dict[str, int]:
    """Hourly (beat ``accounts-cleanup-auth``): delete ``SmsCode`` rows older than 24 h, ``done``/``dead``
    ``sms.send`` outbox rows older than 24 h and customer sessions revoked or expired for more than 90
    days."""
    instant = timeutils.now()
    codes, _ = SmsCode.objects.filter(created_at__lt=instant - SMS_RETENTION).delete()
    messages, _ = OutboxMessage.objects.filter(
        topic=topics.SMS_SEND, status__in=("done", "dead"), updated_at__lt=instant - SMS_RETENTION
    ).delete()
    horizon = instant - SESSION_RETENTION
    sessions, _ = (
        AuthSession.objects.filter(audience="customer")
        .filter(Q(revoked_at__lt=horizon) | Q(revoked_at__isnull=True, absolute_expires_at__lt=horizon))
        .delete()
    )
    return {"sms_codes": codes, "outbox_messages": messages, "sessions": sessions}
