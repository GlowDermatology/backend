"""Who a one-time code is for (customer contract §2.1.1): the phone or e-mail target, its normalisation, the
other identifiers of the same account and the active customer behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from glowderma.accounts.models import Customer, SmsCode
from glowderma.accounts.services.otp import channels
from glowderma.core import enums
from glowderma.core.pii import normalize_email as pii_normalize_email
from glowderma.core.pii import pii_ref

# OtpPurpose (API) → SmsCode.Purpose (stored), customer contract §2.1.
OTP_PURPOSES: dict[str, str] = {
    enums.OtpPurpose.REGISTER: SmsCode.Purpose.SIGNUP,
    enums.OtpPurpose.LOGIN_NEW_DEVICE: SmsCode.Purpose.NEW_DEVICE,
    enums.OtpPurpose.RESET_PASSWORD: SmsCode.Purpose.RESET,
    enums.OtpPurpose.LINK_SOCIAL: SmsCode.Purpose.SOCIAL_LINK,
    enums.OtpPurpose.VERIFY_EMAIL: SmsCode.Purpose.VERIFY_EMAIL,
    enums.OtpPurpose.ATTACH_PHONE: SmsCode.Purpose.ATTACH_PHONE,
}


def normalize_email(value: str) -> str:
    """The stored form of an e-mail address (``core.pii.normalize_email``: NFC, lower case, punycode domain;
    one account per mailbox, C§2.1)."""
    return pii_normalize_email(value)


@dataclass(frozen=True, slots=True)
class OtpTarget:
    """Where a code goes: a phone (``sms``, E.164) or an e-mail address (``email``, normalised)."""

    channel: str
    value: str

    @property
    def is_email(self) -> bool:
        return self.channel == channels.EMAIL_CHANNEL

    @property
    def lookup(self) -> dict[str, str]:
        """The ``SmsCode`` / ``Customer`` filter of this target."""
        return {"email": self.value} if self.is_email else {"phone": self.value}

    def codes(self) -> Any:
        return SmsCode.objects.filter(**self.lookup)

    @property
    def lock_key(self) -> str:
        # The phone key is unchanged ("otp:+998…"); an address can never collide with it.
        return f"otp:email:{self.value}" if self.is_email else f"otp:{self.value}"

    @property
    def log_ref(self) -> str:
        return f"{'email' if self.is_email else 'phone'}_ref={pii_ref(self.value)}"


def otp_target(identifier: str | OtpTarget) -> OtpTarget:
    """A target from a validated identifier: an address holds ``@``, an E.164 phone never does."""
    if isinstance(identifier, OtpTarget):
        return identifier
    if "@" in identifier:
        return OtpTarget(channels.EMAIL_CHANNEL, normalize_email(identifier))
    return OtpTarget(channels.SMS_CHANNEL, identifier)


def row_identifier(row: SmsCode) -> str:
    """The phone or the e-mail address a code row was sent to."""
    return row.email or row.phone


def account_targets(target: OtpTarget) -> list[OtpTarget]:
    """The target and, when it belongs to an active account, the account's other identifier (its phone or
    e-mail): the lockout and the daily wrong-code cap are per account, so an account with both does not get
    two budgets. An unknown target is only itself (nothing is revealed)."""
    customer = Customer.objects.filter(**target.lookup, status=enums.CustomerStatus.ACTIVE).first()
    targets = [target]
    if customer is not None:
        if customer.phone and customer.phone != target.value:
            targets.append(OtpTarget(channels.SMS_CHANNEL, customer.phone))
        if customer.email and customer.email != target.value:
            targets.append(OtpTarget(channels.EMAIL_CHANNEL, customer.email))
    return targets


def lock_target(target: OtpTarget) -> None:
    """``pg_advisory_xact_lock(hashtext('otp:' + phone))`` (``'otp:email:' + address`` for e-mail) for the
    target and its account's other identifier, in sorted order (no deadlock): serialises every OTP decision of
    one account until the surrounding transaction ends (customer contract §2.1.1)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("the per-target OTP lock needs a transaction")
    with transaction.get_connection().cursor() as cursor:
        for key in sorted({each.lock_key for each in account_targets(target)}):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def active_customer_by_phone(identifier: str | OtpTarget) -> Customer | None:
    """The active customer of a phone — or, given an address (hybrid identity), of an e-mail."""
    target = otp_target(identifier)
    return Customer.objects.filter(**target.lookup, status=enums.CustomerStatus.ACTIVE).first()


def active_customer_by_email(email: str) -> Customer | None:
    return active_customer_by_phone(OtpTarget(channels.EMAIL_CHANNEL, normalize_email(email)))


def check_channel(purpose: str, target: OtpTarget) -> None:
    """A purpose bound to one channel refuses the other (a programming error: the serializers refuse it)."""
    channel = channels.CHANNEL_ONLY_PURPOSES.get(purpose)
    if channel is not None and channel != target.channel:
        raise ValueError(f"{purpose!r} codes go by {channel} only")


__all__ = [
    "OTP_PURPOSES",
    "normalize_email",
    "OtpTarget",
    "otp_target",
    "row_identifier",
    "account_targets",
    "lock_target",
    "active_customer_by_phone",
    "active_customer_by_email",
    "check_channel",
]
