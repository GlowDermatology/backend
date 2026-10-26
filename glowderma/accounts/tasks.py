"""Background work of ``accounts``: the ``push.session_replaced`` handler (and its dead-letter hook), the
``accounts.cleanup_auth`` job, and (P1) the ``sms.send`` and ``apple.revoke`` handlers with the
``apple.revoke`` dead-letter hook.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

from glowderma.accounts import services, topics
from glowderma.accounts.models import AuthSession, Device, SmsCode
from glowderma.core import enums, i18n, outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as integration_services
from glowderma.integrations.eskiz.ports import EskizRejected, EskizUnavailable, SmsMessage
from glowderma.integrations.eskiz.registry import get_eskiz_port
from glowderma.integrations.fcm.ports import PAYLOAD_INVALID, UNREGISTERED, PushMessage
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.integrations.oauth.ports import OAuthExchangeError
from glowderma.integrations.oauth.registry import get_apple_service

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


@outbox.on_dead(topics.PUSH_SESSION_REPLACED)
def blank_dead_session_replaced_token(message: OutboxMessage) -> None:
    """A ``push.session_replaced`` that died (FCM unavailable until the last attempt) keeps no token either
    (customer contract §1.9 step 4: the token does not stay behind in the outbox row)."""
    payload: dict[str, Any] = dict(message.payload) if isinstance(message.payload, dict) else {}
    if payload.get("fcm_token"):
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


# --- P1: sms.send and apple.revoke (customer contract §2.14.1, §2.1.8) --------------------------------------


def _payload(message: OutboxMessage) -> dict[str, Any]:
    return dict(message.payload) if isinstance(message.payload, dict) else {}


def sms_text(code: str, lang: str, platform: str) -> str:
    """``sms.otp`` in the request language; Android gets the SMS Retriever app hash on a second line."""
    text = i18n.t("sms.otp", lang, code=code)
    app_hash = str(settings.ANDROID_SMS_APP_HASH)
    if platform == enums.OrderChannel.ANDROID and app_hash:
        text = f"{text}\n{app_hash}"
    return text


@outbox.register(topics.SMS_SEND)
def send_sms(message: OutboxMessage) -> None:
    """Send one OTP (customer contract §2.14.1).

    ``sent_at`` is claimed in its own transaction **before** the provider call, so a redelivery never sends
    twice. The plain code exists only as the sealed Redis payload: missing (expired or flushed) → nothing is
    sent (INFO; the user resends after 60 s). A connect failure (nothing left the process) un-claims and
    re-raises (outbox retry, the payload is kept); a timeout or 5xx after sending keeps the claim and is never
    resent; a 4xx is logged with ``IntegrationState(eskiz)`` and not retried. After a definitive outcome the
    Redis key is deleted.
    """
    payload = _payload(message)
    sms_code_id = int(payload.get("sms_code_id") or 0)
    with transaction.atomic():
        claimed = SmsCode.objects.filter(pk=sms_code_id, sent_at__isnull=True).update(sent_at=timeutils.now())
    if not claimed:
        logger.info("sms.send already claimed sms_code_id=%s", sms_code_id)
        return
    key = services.otp_payload_key(sms_code_id)
    sealed = cache.get(key)
    if not isinstance(sealed, str):
        logger.info("sms.send payload missing sms_code_id=%s", sms_code_id)
        return
    row = SmsCode.objects.get(pk=sms_code_id)
    try:
        code = secretbox.open_(sealed, aad=str(sms_code_id).encode()).decode("ascii")
    except secretbox.SecretboxError:
        logger.error("sms.send payload does not open sms_code_id=%s", sms_code_id)
        cache.delete(key)
        return
    text = sms_text(code, str(payload.get("lang") or "uz"), str(payload.get("platform") or ""))
    try:
        message_id = get_eskiz_port().send(SmsMessage(phone=row.phone, text=text, sms_code_id=sms_code_id))
    except EskizUnavailable as exc:
        if not exc.sent:
            SmsCode.objects.filter(pk=sms_code_id).update(sent_at=None)
            raise
        logger.warning("sms.send outcome unknown sms_code_id=%s (not resent)", sms_code_id)
    except EskizRejected as exc:
        logger.warning("sms.send rejected sms_code_id=%s", sms_code_id)
        integration_services.record_failure("eskiz", f"rejected: {exc}")
    else:
        SmsCode.objects.filter(pk=sms_code_id).update(provider_message_id=message_id[:64])
    cache.delete(key)


@outbox.register(topics.APPLE_REVOKE)
def revoke_apple_token(message: OutboxMessage) -> None:
    """Revoke a sealed Apple refresh token at ``https://appleid.apple.com/auth/revoke`` with the ``client_id``
    it was issued to (customer contract §2.1.8). Transport errors propagate (outbox retry); an Apple 4xx (the
    token is already invalid) is final with a WARNING. After a final outcome the sealed token is blanked
    in the
    stored payload."""
    payload = _payload(message)
    sealed = str(payload.get("sealed_token") or "")
    if not sealed:
        return
    client_id = str(payload.get("client_id") or "")
    try:
        refresh_token = services.open_apple_refresh(sealed, client_id=client_id)
    except secretbox.SecretboxError:
        logger.error("apple.revoke token does not open identity_id=%s", payload.get("identity_id"))
    else:
        try:
            get_apple_service(require_available=False).revoke(refresh_token, client_id=client_id)
        except OAuthExchangeError as exc:
            if exc.kind == "unavailable":
                raise
            logger.warning("apple.revoke refused identity_id=%s (final)", payload.get("identity_id"))
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, "sealed_token": ""}, updated_at=timeutils.now()
    )


@outbox.on_dead(topics.APPLE_REVOKE)
def apple_revoke_dead(message: OutboxMessage) -> None:
    """Apple stayed unreachable until the last attempt: ERROR ``apple.revoke_dead`` (alert; staff revoke the
    token in the Apple developer account)."""
    logger.error("apple.revoke_dead identity_id=%s", _payload(message).get("identity_id"))
