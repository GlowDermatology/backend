"""Delivering one-time codes: the ``sms.send`` (DevSMS) and ``email.send`` handlers."""

from __future__ import annotations

import logging
import uuid

from django.core.cache import cache
from django.db import transaction

from glowderma.accounts import services, topics
from glowderma.accounts.models import SmsCode
from glowderma.accounts.tasks import message_rendering, outbox_payloads
from glowderma.core import outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as integration_services
from glowderma.integrations.devsms.ports import OtpPurpose, OtpSms, SmsRejected, SmsUnavailable
from glowderma.integrations.devsms.registry import get_sms_port
from glowderma.integrations.email.ports import EmailMessage, EmailRejected, EmailUnavailable
from glowderma.integrations.email.registry import get_email_port

logger = logging.getLogger("glowderma.accounts.tasks")

# The universal OTP template of each SMS code (docs/sms-template-audit.md): the provider's text says what the
# code is for, so every stored purpose an SMS can carry maps to one. ``verify_email`` is e-mail only.
SMS_PURPOSES: dict[str, OtpPurpose] = {
    SmsCode.Purpose.SIGNUP: OtpPurpose.REGISTRATION,
    SmsCode.Purpose.NEW_DEVICE: OtpPurpose.LOGIN,
    SmsCode.Purpose.RESET: OtpPurpose.PASSWORD_RESET,
    # completing a social sign-in with the phone (a new account or a link to the phone's account)
    SmsCode.Purpose.SOCIAL_LINK: OtpPurpose.ACTION_CONFIRMATION,
    # adding a phone to a signed-in account
    SmsCode.Purpose.ATTACH_PHONE: OtpPurpose.ACTION_CONFIRMATION,
}
IDEMPOTENCY_NAMESPACE = uuid.UUID("5c1f3e0a-2d7b-5e4f-9a61-0b8d3c2e7f45")


def sms_idempotency_key(row: SmsCode) -> str:
    """One provider idempotency key per code, the same for every delivery attempt of it. DevSMS keeps keys for
    ever and shares them across the account (staging and production may share one), so the key covers the
    row's creation instant too, not only its id."""
    return str(uuid.uuid5(IDEMPOTENCY_NAMESPACE, f"sms_code:{row.pk}:{row.created_at.isoformat()}"))


@outbox.register(topics.SMS_SEND)
def send_sms(message: OutboxMessage) -> None:
    """Send one OTP (customer contract §2.14.1).

    The sealed Redis payload and the ``SmsCode`` row are read **before** ``sent_at`` is claimed, so a Redis
    or database error on the way fails the delivery while nothing is claimed (the outbox retries and the
    retry still sends). ``sent_at`` is then claimed in its own transaction before the provider call, so a
    redelivery never sends twice. The plain code exists only as the sealed Redis payload: missing (expired
    or flushed) → nothing is sent (INFO; the user resends after 60 s). The code goes through the universal
    OTP template of its purpose (:data:`SMS_PURPOSES`) with the code's idempotency key: an outcome that is
    safe to repeat (no answer, a timeout, 5xx, 429; DevSMS replays a repeated key without sending or
    charging) un-claims and re-raises (outbox retry with the same code, the payload is kept); DevSMS's own
    "outcome unknown" keeps the claim and is never resent; a refusal is logged with
    ``IntegrationState(devsms)`` and not retried. After a definitive outcome the Redis key is deleted.
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
    purpose = SMS_PURPOSES.get(row.purpose)
    if purpose is None:
        logger.error("sms.send no SMS template for purpose=%s sms_code_id=%s", row.purpose, sms_code_id)
        cache.delete(key)
        return
    with transaction.atomic():
        claimed = SmsCode.objects.filter(pk=sms_code_id, sent_at__isnull=True).update(sent_at=timeutils.now())
    if not claimed:
        logger.info("sms.send already claimed sms_code_id=%s", sms_code_id)
        return
    sms = OtpSms(
        phone=row.phone,
        code=code,
        purpose=purpose,
        sms_code_id=sms_code_id,
        idempotency_key=sms_idempotency_key(row),
    )
    try:
        delivery = get_sms_port().send_otp(sms)
    except SmsUnavailable as exc:
        if not exc.sent:
            SmsCode.objects.filter(pk=sms_code_id).update(sent_at=None)
            raise
        logger.warning("sms.send outcome unknown sms_code_id=%s (not resent)", sms_code_id)
        if exc.message_id:  # DevSMS's id of the charged attempt, for a status check
            SmsCode.objects.filter(pk=sms_code_id).update(provider_message_id=exc.message_id[:64])
    except SmsRejected as exc:
        logger.warning("sms.send rejected sms_code_id=%s reason=%s", sms_code_id, exc.reason)
        integration_services.record_failure("devsms", f"rejected: {exc}")
    else:
        SmsCode.objects.filter(pk=sms_code_id).update(provider_message_id=delivery.message_id[:64])
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
    "IDEMPOTENCY_NAMESPACE",
    "SMS_PURPOSES",
    "sms_idempotency_key",
    "send_sms",
    "send_email_code",
]
