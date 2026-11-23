"""Checking a one-time code and the short-lived verification token it yields (customer contract §2.1.2): the
wrong-code lockout, the token lookup and its single use.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from glowderma.accounts.models import SmsCode
from glowderma.accounts.services.common import hashing
from glowderma.accounts.services.otp import issuance, limits, targets
from glowderma.core import timeutils
from glowderma.core.api.exceptions import OtpExpired, OtpInvalid, OtpLocked, VerificationInvalid


@dataclass(frozen=True, slots=True)
class Verification:
    """``auth/verify-code/`` 200: the raw token is returned once; only its SHA-256 is stored."""

    token: str
    expires_in: int
    purpose: str
    customer_exists: bool


def verify_code(identifier: str | targets.OtpTarget, purpose: str, code: str) -> Verification:
    """``auth/verify-code/`` (customer contract §2.1.2) under the per-target advisory lock and a row lock.

    No live code (none, expired, consumed, superseded) → ``otp_expired``; exhausted row, lockout or daily
    wrong-code cap → ``otp_locked``; HMAC mismatch → ``attempts += 1`` (``exhausted_at`` at the max) is
    **committed** and then ``otp_invalid`` (``attempts_left`` = min(this code's, the day's)) or, at 0 left,
    ``otp_locked``. A match issues the single-use verification token (SHA-256 stored, 10 min).
    """
    target = targets.otp_target(identifier)
    stored = targets.OTP_PURPOSES[purpose]
    now = timeutils.now()
    refusal: Exception | None = None
    with transaction.atomic():
        targets.lock_target(target)
        row = target.codes().select_for_update().filter(purpose=stored).order_by("-created_at", "-id").first()
        if row is None or row.consumed_at is not None or row.expires_at <= now:
            raise OtpExpired()
        block = limits.phone_block(target, now)
        if row.exhausted_at is not None or block is not None:
            raise block or OtpLocked(retry_after=60 * int(settings.CUSTOMER_OTP_LOCKOUT_MINUTES))
        if not hmac.compare_digest(row.code_hmac, issuance.code_hmac(code)):
            row.attempts += 1
            if row.attempts >= row.max_attempts:
                row.exhausted_at = now
            row.save(update_fields=["attempts", "exhausted_at"])
            day_left = int(settings.CUSTOMER_OTP_DAILY_WRONG_LIMIT) - limits.wrong_codes_today(target, now)
            left = min(row.max_attempts - row.attempts, day_left)
            if left <= 0:
                refusal = limits.phone_block(target, now) or OtpLocked(retry_after=limits.to_midnight(now))
            else:
                refusal = OtpInvalid(details={"attempts_left": left}, params={"n": left})
        else:
            token = secrets.token_urlsafe(32)
            ttl = int(settings.CUSTOMER_VERIFICATION_TTL_SECONDS)
            row.consumed_at = now
            row.verification_token_hash = hashing.hash_secret(token)
            row.verification_expires_at = now + timedelta(seconds=ttl)
            row.save(update_fields=["consumed_at", "verification_token_hash", "verification_expires_at"])
            return Verification(token, ttl, purpose, targets.active_customer_by_phone(target) is not None)
    raise refusal


def verification_row(
    token: str,
    *,
    purpose: str,
    phone: str | None = None,
    lock: bool = False,
    email: str | None = None,
) -> SmsCode:
    """The usable ``SmsCode`` of a verification token (unused, unexpired, of ``purpose`` and, when given,
    of ``phone`` / ``email``), else 400 ``verification_invalid``. With ``lock`` the row is locked (``FOR
    UPDATE``) for :func:`consume_verification`."""
    if not token:
        raise VerificationInvalid()
    queryset = SmsCode.objects.filter(verification_token_hash=hashing.hash_secret(token))
    if lock:
        queryset = queryset.select_for_update()
    row = queryset.first()
    if (
        row is None
        or row.purpose != targets.OTP_PURPOSES.get(purpose)
        or row.verification_used_at is not None
        or row.verification_expires_at is None
        or row.verification_expires_at <= timeutils.now()
        or (phone is not None and row.phone != phone)
        or (email is not None and row.email != targets.normalize_email(email))
    ):
        raise VerificationInvalid()
    return row


def consume_verification(
    token: str, *, purpose: str, phone: str | None = None, email: str | None = None
) -> tuple[str, int]:
    """Mark a verification token used under its row lock (single use; part of the caller's transaction, so a
    refusal later in the flow rolls the use back). Returns ``(phone or e-mail address, sms_code_id)``."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("consume_verification needs the caller's transaction")
    row = verification_row(token, purpose=purpose, phone=phone, email=email, lock=True)
    row.verification_used_at = timeutils.now()
    row.save(update_fields=["verification_used_at"])
    return targets.row_identifier(row), row.pk


__all__ = ["Verification", "verify_code", "verification_row", "consume_verification"]
