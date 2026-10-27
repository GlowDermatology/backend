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
from django.template.loader import render_to_string

from glowderma.accounts import services, topics
from glowderma.accounts.models import AuthSession, Device, SmsCode
from glowderma.core import enums, i18n, integration_modes, outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as integration_services
from glowderma.integrations.email.ports import EmailMessage, EmailRejected, EmailUnavailable
from glowderma.integrations.email.registry import get_email_port
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
    """Hourly (beat ``accounts-cleanup-auth``): delete ``SmsCode`` rows (SMS and e-mail codes) older than
    24 h, ``done``/``dead`` ``sms.send`` and ``email.send`` outbox rows older than 24 h and customer sessions
    revoked or expired for more than 90 days."""
    instant = timeutils.now()
    codes, _ = SmsCode.objects.filter(created_at__lt=instant - SMS_RETENTION).delete()
    messages, _ = OutboxMessage.objects.filter(
        topic__in=(topics.SMS_SEND, topics.EMAIL_SEND),
        status__in=("done", "dead"),
        updated_at__lt=instant - SMS_RETENTION,
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

    The sealed Redis payload and the ``SmsCode`` row are read **before** ``sent_at`` is claimed, so a Redis
    or database error on the way fails the delivery while nothing is claimed (the outbox retries and the
    retry still sends). ``sent_at`` is then claimed in its own transaction before the provider call, so a
    redelivery never sends twice. The plain code exists only as the sealed Redis payload: missing (expired
    or flushed) → nothing is sent (INFO; the user resends after 60 s). A connect failure (nothing left the
    process) un-claims and re-raises (outbox retry, the payload is kept); a timeout or 5xx after sending
    keeps the claim and is never resent; a 4xx is logged with ``IntegrationState(eskiz)`` and not retried.
    After a definitive outcome the Redis key is deleted.
    """
    payload = _payload(message)
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
    text = sms_text(code, str(payload.get("lang") or "uz"), str(payload.get("platform") or ""))
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


def email_parts(code: str, lang: str, purpose: str) -> tuple[str, str, str]:
    """``(subject, text, html)`` of an e-mail code in the request language (uz/ru/en catalog texts,
    ``accounts/email/otp.txt`` and ``.html``)."""
    lang = i18n.normalize_language(lang)
    intro_key = f"email.otp.intro.{purpose}"
    if intro_key not in i18n.CATALOG:
        intro_key = "email.otp.intro.verify_email"
    minutes = max(1, int(settings.CUSTOMER_OTP_TTL_SECONDS) // 60)
    context = {
        "lang": lang,
        "subject": i18n.t("email.otp.subject", lang),
        "intro": i18n.t(intro_key, lang),
        "code": code,
        "expiry": i18n.t("email.otp.expiry", lang, m=minutes),
        "ignore": i18n.t("email.otp.ignore", lang),
        "footer": i18n.t("email.otp.footer", lang),
    }
    text = render_to_string("accounts/email/otp.txt", context).strip() + "\n"
    html = render_to_string("accounts/email/otp.html", context)
    return str(context["subject"]), text, html


@outbox.register(topics.EMAIL_SEND)
def send_email_code(message: OutboxMessage) -> None:
    """Send one e-mail OTP (hybrid identity, customer contract §2.14.4): the ``sms.send`` handler's rules
    for an e-mail code — sealed payload and row read before ``sent_at`` is claimed, the claim in its own
    transaction before the relay call (a redelivery never sends twice), a missing payload sends nothing, a
    relay that accepted nothing un-claims and re-raises (outbox retry), an unknown outcome keeps the claim and
    is never resent, a refusal is logged and final; the Redis key is deleted after a definitive outcome. Log
    lines carry the code id only — never the address, the subject or the code."""
    payload = _payload(message)
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
    subject, text, html = email_parts(
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


@outbox.register(topics.EMAIL_NOTICE)
def send_email_notice(message: OutboxMessage) -> None:
    """A security notice e-mail (hybrid identity, C§2.2.10: the previous address of a replaced e-mail). The
    address is sealed in the payload and blanked after a definitive outcome; a relay that accepted nothing
    re-raises (outbox retry), an unknown outcome or a refusal is final. Logs carry the outbox id only."""
    payload = _payload(message)
    sealed = str(payload.get("to") or "")
    if not sealed:
        return
    kind = str(payload.get("kind") or "")
    try:
        address = secretbox.open_(sealed, aad=services.EMAIL_NOTICE_AAD).decode("utf-8")
    except secretbox.SecretboxError:
        logger.error("email.notice address does not open outbox_id=%s", message.pk)
        address = ""
    if address and kind in services.EMAIL_NOTICE_KINDS:
        lang = i18n.normalize_language(payload.get("lang"))
        context = {
            "lang": lang,
            "subject": i18n.t(f"email.notice.{kind}.subject", lang),
            "body": i18n.t(f"email.notice.{kind}.body", lang),
            "footer": i18n.t("email.otp.footer", lang),
        }
        email = EmailMessage(
            to=address,
            subject=str(context["subject"]),
            text=render_to_string("accounts/email/notice.txt", context).strip() + "\n",
            html=render_to_string("accounts/email/notice.html", context),
            reference=f"outbox_id={message.pk}",
        )
        try:
            get_email_port().send(email)
        except EmailUnavailable as exc:
            if not exc.sent:
                raise
            logger.warning("email.notice outcome unknown outbox_id=%s (not resent)", message.pk)
        except EmailRejected:
            logger.warning("email.notice rejected outbox_id=%s", message.pk)
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, "to": ""}, updated_at=timeutils.now()
    )


class AppleRevokeNotSent(RuntimeError):
    """The revocation was not sent: OAuth runs its fake adapter under the production settings."""


def _apple_grant(payload: dict[str, Any]) -> str:
    """The log reference of an ``apple.revoke`` grant: identity, customer and ``pii_ref`` of Apple's ``sub``
    (never the subject itself or the token)."""
    identity_id, customer_id = payload.get("identity_id"), payload.get("customer_id")
    return (
        f"identity_id={identity_id} customer_id={customer_id} subject_ref={payload.get('subject_ref') or '-'}"
    )


@outbox.register(topics.APPLE_REVOKE)
def revoke_apple_token(message: OutboxMessage) -> None:
    """Revoke a sealed Apple refresh token at ``https://appleid.apple.com/auth/revoke`` with the ``client_id``
    it was issued to (customer contract §2.1.8). Transport errors and Apple misconfiguration answers
    (``unavailable``) propagate (outbox retry, then ``apple.revoke_dead``); an Apple 400 for a token that is
    already invalid is final with a WARNING. After a final outcome the sealed token and the subject are
    blanked in the stored payload (``customer_id`` and ``subject_ref`` stay for the logs).

    A grant whose Apple ``sub`` is linked to a customer again at delivery (the account re-linked the same
    Apple ID, or the refused flow's subject was linked meanwhile) is skipped as final: Apple's revocation ends
    the user's whole authorization of the app, which would invalidate the linked identity's own token.

    While OAuth is fake under the production settings (C§6.14) the fake is never called — it would report a
    revocation that did not happen: ERROR ``apple.revoke_not_sent`` and the handler raises, so the sealed
    token stays in the payload, the outbox retries (OAuth may be live again by then) and a message that
    never gets through ends ``dead`` with the ``apple.revoke_dead`` alert (staff revoke by hand, finding the
    grant by ``customer_id`` and ``subject``).
    """
    payload = _payload(message)
    sealed = str(payload.get("sealed_token") or "")
    if not sealed:
        return
    grant = _apple_grant(payload)
    if services.apple_subject_linked(str(payload.get("subject") or "")):
        logger.info("apple.revoke skipped %s (the subject is linked again; final)", grant)
    else:
        _revoke_apple_grant(payload, sealed, grant)
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, "sealed_token": "", "subject": ""}, updated_at=timeutils.now()
    )


def _revoke_apple_grant(payload: dict[str, Any], sealed: str, grant: str) -> None:
    if integration_modes.fake_in_production("oauth"):
        logger.error("apple.revoke_not_sent %s (OAuth is fake under the production settings)", grant)
        raise AppleRevokeNotSent(f"apple.revoke not sent: {grant}")
    client_id = str(payload.get("client_id") or "")
    try:
        refresh_token = services.open_apple_refresh(sealed, client_id=client_id)
    except secretbox.SecretboxError:
        logger.error("apple.revoke token does not open %s", grant)
        return
    try:
        get_apple_service(require_available=False).revoke(refresh_token, client_id=client_id)
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise
        logger.warning("apple.revoke refused %s (final)", grant)


@outbox.on_dead(topics.APPLE_REVOKE)
def apple_revoke_dead(message: OutboxMessage) -> None:
    """Apple stayed unreachable until the last attempt: ERROR ``apple.revoke_dead`` with the grant's
    reference (alert; staff revoke the token in the Apple developer account)."""
    logger.error("apple.revoke_dead %s", _apple_grant(_payload(message)))
