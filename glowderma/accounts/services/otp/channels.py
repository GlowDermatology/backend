"""The two OTP delivery channels (customer contract §2.1.1): SMS to a phone number and e-mail to an address,
and the purposes that only one of them may carry.

:class:`OtpChannel` holds what differs between the two channels as data (the Strategy the limit checks look
up by :data:`CHANNELS`), so the checks themselves have no per-channel branches. What differs in *behaviour*
— the payload a channel sends, its extra per-IP cap — stays explicit where it happens.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from glowderma.core import enums
from glowderma.core.api.exceptions import EmailServiceUnavailable, SmsUnavailable

SMS_CHANNEL = "sms"
EMAIL_CHANNEL = "email"
# Purposes bound to one channel: attaching a phone is proven by SMS, verifying an e-mail by e-mail.
CHANNEL_ONLY_PURPOSES: dict[str, str] = {
    enums.OtpPurpose.ATTACH_PHONE: SMS_CHANNEL,
    enums.OtpPurpose.VERIFY_EMAIL: EMAIL_CHANNEL,
}


@dataclass(frozen=True, slots=True)
class OtpChannel:
    """The per-channel settings of the OTP limits."""

    name: str
    # Cache-key prefix of the channel's global hourly counter (``<prefix>:<YYYYMMDDHH>``, Tashkent time).
    global_counter_prefix: str
    # Name of the setting that caps that counter; read when checked, so an override applies at once.
    global_limit_setting: str
    # ERROR log event and alert name when the hourly breaker trips.
    budget_event: str
    # The 503 answered while the breaker is tripped.
    unavailable: Callable[..., Exception]
    # Message key of the daily-limit refusal (``None``: the generic one).
    daily_limit_key: str | None


SMS = OtpChannel(
    name=SMS_CHANNEL,
    global_counter_prefix="gd:otp:global",
    global_limit_setting="SMS_GLOBAL_HOURLY_LIMIT",
    budget_event="otp.budget_tripped",
    unavailable=SmsUnavailable,
    daily_limit_key=None,
)
EMAIL = OtpChannel(
    name=EMAIL_CHANNEL,
    global_counter_prefix="gd:otp:email_global",
    global_limit_setting="EMAIL_GLOBAL_HOURLY_LIMIT",
    budget_event="otp.email_budget_tripped",
    unavailable=EmailServiceUnavailable,
    daily_limit_key="error.otp_daily_limit_email",
)
CHANNELS: dict[str, OtpChannel] = {SMS.name: SMS, EMAIL.name: EMAIL}


__all__ = ["SMS_CHANNEL", "EMAIL_CHANNEL", "CHANNEL_ONLY_PURPOSES", "OtpChannel", "SMS", "EMAIL", "CHANNELS"]
