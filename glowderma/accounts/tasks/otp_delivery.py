"""Delivering one-time codes: the ``sms.send`` (Eskiz) and ``email.send`` handlers."""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction

from glowderma.accounts import services, topics
from glowderma.accounts.models import SmsCode
from glowderma.accounts.tasks import message_rendering, outbox_payloads
from glowderma.core import outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as integration_services
from glowderma.integrations.email.ports import EmailMessage, EmailRejected, EmailUnavailable
from glowderma.integrations.email.registry import get_email_port
from glowderma.integrations.eskiz.ports import EskizRejected, EskizUnavailable, SmsMessage
from glowderma.integrations.eskiz.registry import get_eskiz_port

logger = logging.getLogger("glowderma.accounts.tasks")


@outbox.register(topics.SMS_SEND)
def send_sms(message: OutboxMessage) -> None:
    """Send one OTP (customer contract §2.14.1).

    The sealed Redis payload and the ``SmsCode`` row are read **before** ``sent_at`` is claimed, so a Redis
    or database error on the way fails the delivery while nothing is claimed (the outbox retries and the
    retry still sends). ``sent_at`` is then claimed in its own transaction before the provider call, so a
    redelivery never sends twice. The plain code exists only as the sealed Redis payload: missing (expired
    or flushed) → nothing is sent (INFO; the user resends after 60 s). A connect failure (nothing left the
    process) un-claims and re-raises (outbox retry, the payload is kept); a timeout or 5xx after sending
    keeps the claim and is never resent; a 4xx is logged with ``IntegrationState(eskiz)`` and not retried.
    After a definitive outcome the Redis key is deleted.
    """
    payload = outbox_payloads.read_payload(message)
    sms_code_id = int(payload.get("sms_code_id") or 0)
    key = services.otp_payload_key(sms_code_id)
    sealed = cache.get(key)
    if not isinstance(sealed, str):
        logger.info("sms.send payload missing sms_code_id=%s", sms_code_id)
        return
    row = SmsCode.objects.filter(pk=sms_code_id).exclude(phone="").first()
    if row is None:
        logger.info("sms.send code row missing sms_code_id=%s", sms_code_id)
        return
    try:
        code = secretbox.open_(sealed, aad=str(sms_code_id).encode()).decode("ascii")
    except secretbox.SecretboxError:
        logger.error("sms.send payload does not open sms_code_id=%s", sms_code_id)
        cache.delete(key)
        return
    text = message_rendering.sms_text(
        code, str(payload.get("lang") or "uz"), str(payload.get("platform") or "")
    )
    with transaction.atomic():
        claimed = SmsCode.objects.filter(pk=sms_code_id, sent_at__isnull=True).update(sent_at=timeutils.now())
    if not claimed:
        logger.info("sms.send already claimed sms_code_id=%s", sms_code_id)
        return
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
    try:
        cache.delete(key)
    except Exception:  # the SMS went out; the sealed payload expires with the code anyway
        logger.warning("sms.send payload not deleted sms_code_id=%s", sms_code_id, exc_info=True)


@outbox.register(topics.EMAIL_SEND)
def send_email_code(message: OutboxMessage) -> None:
    """Send one e-mail OTP (hybrid identity, customer contract §2.14.4): the ``sms.send`` handler's rules
    for an e-mail code — sealed payload and row read before ``sent_at`` is claimed, the claim in its own
    transaction before the relay call (a redelivery never sends twice), a missing payload sends nothing, a
    relay that accepted nothing un-claims and re-raises (outbox retry), an unknown outcome keeps the claim and
    is never resent, a refusal is logged and final; the Redis key is deleted after a definitive outcome. Log
    lines carry the code id only — never the address, the subject or the code."""
    payload = outbox_payloads.read_payload(message)
    sms_code_id = int(payload.get("sms_code_id") or 0)
    key = services.otp_payload_key(sms_code_id)
    sealed = cache.get(key)
    if not isinstance(sealed, str):
        logger.info("email.send payload missing sms_code_id=%s", sms_code_id)
        return
    row = SmsCode.objects.filter(pk=sms_code_id).exclude(email="").first()
    if row is None:
        logger.info("email.send code row missing sms_code_id=%s", sms_code_id)
        return
    try:
        code = secretbox.open_(sealed, aad=str(sms_code_id).encode()).decode("ascii")
    except secretbox.SecretboxError:
        logger.error("email.send payload does not open sms_code_id=%s", sms_code_id)
        cache.delete(key)
        return
    subject, text, html = message_rendering.email_parts(
        code, str(payload.get("lang") or "uz"), str(payload.get("purpose") or "")
    )
    with transaction.atomic():
        claimed = SmsCode.objects.filter(pk=sms_code_id, sent_at__isnull=True).update(sent_at=timeutils.now())
    if not claimed:
        logger.info("email.send already claimed sms_code_id=%s", sms_code_id)
        return
    email = EmailMessage(
        to=row.email, subject=subject, text=text, html=html, reference=f"sms_code_id={sms_code_id}"
    )
    try:
        message_id = get_email_port().send(email)
    except EmailUnavailable as exc:
        if not exc.sent:
            SmsCode.objects.filter(pk=sms_code_id).update(sent_at=None)
            raise
        logger.warning("email.send outcome unknown sms_code_id=%s (not resent)", sms_code_id)
    except EmailRejected:
        logger.warning("email.send rejected sms_code_id=%s", sms_code_id)
    else:
        SmsCode.objects.filter(pk=sms_code_id).update(provider_message_id=message_id[:64])
    try:
        cache.delete(key)
    except Exception:  # the e-mail went out; the sealed payload expires with the code anyway
        logger.warning("email.send payload not deleted sms_code_id=%s", sms_code_id, exc_info=True)


__all__ = [
    "send_sms",
    "send_email_code",
]
