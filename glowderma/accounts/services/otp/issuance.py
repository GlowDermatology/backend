"""Issuing one-time codes (customer contract §2.1.1): the stored HMAC of the code, the sealed outbox payload
the SMS / e-mail handler sends, and the limit checks that run before a code is created.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from glowderma.accounts import topics
from glowderma.accounts.models import SmsCode
from glowderma.accounts.services.otp import channels, limits, targets
from glowderma.core import enums, outbox, secretbox, timeutils
from glowderma.core.api.exceptions import (
    EmailNotRegistered,
    EmailRegistered,
    OtpResendTooSoon,
    PhoneNotRegistered,
    PhoneRegistered,
)
from glowderma.core.pii import pii_ref
from glowderma.integrations.email import registry as email_registry
from glowderma.integrations.eskiz import registry as eskiz_registry

logger = logging.getLogger("glowderma.accounts.services")


OTP_CODE_LENGTH = 6
OTP_PAYLOAD_KEY = "gd:otp:payload:{sms_code_id}"
SOCIAL_OTP_KEY = "gd:social:otp:{sms_code_id}"
SOCIAL_OTP_TTL_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class CodeIssue:
    """``auth/send-code/`` 200 and the ``otp`` block of ``NextStep`` (customer contract §2.1.1, §2.1.4)."""

    sms_code_id: int
    phone: str  # "" for an e-mail code
    purpose: str
    expires_in: int
    resend_in: int
    attempts: int
    length: int = OTP_CODE_LENGTH
    email: str = ""  # "" for an SMS code

    @property
    def channel(self) -> str:
        return channels.EMAIL_CHANNEL if self.email else channels.SMS_CHANNEL


def code_hmac(code: str) -> str:
    """``HMAC-SHA256(code, SMS_CODE_HMAC_KEY)`` hex: the only stored form of a code, SMS or e-mail (§6.4)."""
    return hmac.new(
        str(settings.SMS_CODE_HMAC_KEY).encode("utf-8"), code.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def otp_payload_key(sms_code_id: int) -> str:
    return OTP_PAYLOAD_KEY.format(sms_code_id=sms_code_id)


def social_otp_key(sms_code_id: int) -> str:
    return SOCIAL_OTP_KEY.format(sms_code_id=sms_code_id)


def _create_code(
    target: targets.OtpTarget,
    stored_purpose: str,
    *,
    purpose: str,
    ip: str,
    fid: str,
    lang: str,
    platform: str,
    now: datetime,
) -> SmsCode:
    """Supersede the live code, store a new HMAC row, seal the plain code into Redis for the handler, count it
    and enqueue ``sms.send`` / ``email.send`` (no phone, no address, no text, no code in the payload)."""
    target.codes().filter(purpose=stored_purpose, consumed_at__isnull=True, expires_at__gt=now).update(
        expires_at=now
    )
    code = email_registry.new_code() if target.is_email else eskiz_registry.new_code()
    ttl = int(settings.CUSTOMER_OTP_TTL_SECONDS)
    row = SmsCode.objects.create(
        **target.lookup,
        purpose=stored_purpose,
        code_hmac=code_hmac(code),
        max_attempts=int(settings.CUSTOMER_OTP_MAX_ATTEMPTS),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
        request_ip=ip or None,
    )
    cache.set(otp_payload_key(row.pk), secretbox.seal(code.encode("ascii"), aad=str(row.pk).encode()), ttl)
    limits.count_issue(fid=fid, ip=ip, now=now, channel=target.channel)
    if target.is_email:
        outbox.enqueue(
            topics.EMAIL_SEND,
            {"sms_code_id": row.pk, "lang": lang, "purpose": purpose},
            dedupe_key=topics.email_send_key(row.pk),
        )
        logger.info("otp.issued purpose=%s channel=email %s", purpose, target.log_ref)
    else:
        outbox.enqueue(
            topics.SMS_SEND,
            {"sms_code_id": row.pk, "lang": lang, "platform": platform},
            dedupe_key=topics.sms_send_key(row.pk),
        )
        logger.info("otp.issued purpose=%s phone_ref=%s", purpose, pii_ref(target.value))  # contract literal
    return row


def _issue(row: SmsCode, purpose: str, now: datetime) -> CodeIssue:
    resend_at = row.created_at + timedelta(seconds=int(settings.CUSTOMER_OTP_RESEND_SECONDS))
    return CodeIssue(
        sms_code_id=row.pk,
        phone=row.phone,
        email=row.email,
        purpose=purpose,
        expires_in=max(0, int((row.expires_at - now).total_seconds())),
        resend_in=max(0, int((resend_at - now).total_seconds() + 0.999)),
        attempts=row.max_attempts - row.attempts,
    )


def send_code(
    identifier: str | targets.OtpTarget,
    purpose: str,
    *,
    ip: str,
    fid: str,
    lang: str,
    platform: str,
    social_jti: str | None = None,
) -> CodeIssue:
    """``auth/send-code/`` (customer contract §2.1.1), checks in contract order, for a phone or an e-mail.

    ``login_new_device`` is refused (only ``auth/login/`` issues it, after the password). Target state
    (``phone_registered`` / ``phone_not_registered``, ``email_registered`` / ``email_not_registered``), the
    channel's global hourly budget, then under the per-target advisory lock: lockout, daily wrong-code cap,
    resend window, target and fid day caps, IP day cap. For ``link_social`` the ticket's ``jti`` is remembered
    for the code (``gd:social:otp:{id}``, 15 min). ``attach_phone`` / ``verify_email`` (the signed-in
    ``me/phone/code/`` and ``me/email/code/``) check nothing about the target here: their caller refuses a
    phone or an address of another account first (``phone_taken`` / ``email_taken``).
    """
    if purpose not in targets.OTP_PURPOSES or purpose == enums.OtpPurpose.LOGIN_NEW_DEVICE:
        raise ValueError(f"send_code cannot issue {purpose!r}")
    if purpose == enums.OtpPurpose.LINK_SOCIAL and not social_jti:
        raise ValueError("link_social codes need the ticket jti")
    target = targets.otp_target(identifier)
    targets.check_channel(purpose, target)
    customer = targets.active_customer_by_phone(target)
    if purpose == enums.OtpPurpose.REGISTER and customer is not None:
        raise EmailRegistered() if target.is_email else PhoneRegistered()
    if purpose == enums.OtpPurpose.RESET_PASSWORD and customer is None:
        raise EmailNotRegistered() if target.is_email else PhoneNotRegistered()
    now = timeutils.now()
    budget = limits.budget_block(now, target.channel)
    if budget is not None:
        raise budget
    stored = targets.OTP_PURPOSES[purpose]
    with transaction.atomic():
        targets.lock_target(target)
        block = limits.phone_block(target, now)
        if block is not None:
            raise block
        newest = target.codes().filter(purpose=stored).order_by("-created_at", "-id").first()
        resend = timedelta(seconds=int(settings.CUSTOMER_OTP_RESEND_SECONDS))
        if newest is not None and newest.created_at > now - resend:
            raise OtpResendTooSoon(retry_after=limits.seconds(newest.created_at + resend - now))
        block = limits.cap_block(target, fid=fid, ip=ip, now=now)
        if block is not None:
            raise block
        row = _create_code(
            target, stored, purpose=purpose, ip=ip, fid=fid, lang=lang, platform=platform, now=now
        )
        if social_jti:
            cache.set(social_otp_key(row.pk), social_jti, SOCIAL_OTP_TTL_SECONDS)
        return _issue(row, purpose, now)


def otp_blocked(identifier: str | targets.OtpTarget, *, fid: str, ip: str, now: datetime) -> Exception | None:
    """The pre-password check of ``auth/login/`` step 3 (customer contract §2.1.4): takes the per-target
    advisory lock (held until the caller's transaction ends) and returns the refusal ``send_code`` could
    meet — lockout, daily code cap, daily wrong-code cap, fid cap, IP cap (``otp_network_limit``), the
    channel's global budget (``sms_unavailable`` / ``email_unavailable``) — or ``None``. The same answer for
    every password and for unknown phones and addresses."""
    target = targets.otp_target(identifier)
    targets.lock_target(target)
    return (
        limits.phone_block(target, now)
        or limits.cap_block(target, fid=fid, ip=ip, now=now)
        or limits.budget_block(now, target.channel)
    )


def issue_login_code(
    identifier: str | targets.OtpTarget, *, ip: str, fid: str, lang: str, platform: str
) -> CodeIssue:
    """The ``login_new_device`` code of ``auth/login/`` step 6, after the password check, sent to what the
    customer signed in with (phone → SMS, e-mail → e-mail).

    Runs under the advisory lock :func:`otp_blocked` took (the caller's transaction). A live code younger than
    the resend window is reused (nothing sent; ``resend_in`` = the remaining wait); otherwise a new one is
    issued. The fid, IP and global counters are incremented without a second check, so this never answers a
    cap error after the password was checked (a concurrent request may overshoot one of those cost caps by
    one).
    """
    target = targets.otp_target(identifier)
    now = timeutils.now()
    targets.lock_target(target)
    stored = SmsCode.Purpose.NEW_DEVICE
    resend = timedelta(seconds=int(settings.CUSTOMER_OTP_RESEND_SECONDS))
    live = (
        target.codes()
        .filter(
            purpose=stored,
            consumed_at__isnull=True,
            exhausted_at__isnull=True,
            expires_at__gt=now,
            created_at__gt=now - resend,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if live is None:
        live = _create_code(
            target,
            stored,
            purpose=enums.OtpPurpose.LOGIN_NEW_DEVICE,
            ip=ip,
            fid=fid,
            lang=lang,
            platform=platform,
            now=now,
        )
    return _issue(live, enums.OtpPurpose.LOGIN_NEW_DEVICE, now)


__all__ = [
    "OTP_CODE_LENGTH",
    "OTP_PAYLOAD_KEY",
    "SOCIAL_OTP_KEY",
    "SOCIAL_OTP_TTL_SECONDS",
    "code_hmac",
    "otp_payload_key",
    "social_otp_key",
    "CodeIssue",
    "send_code",
    "otp_blocked",
    "issue_login_code",
]
