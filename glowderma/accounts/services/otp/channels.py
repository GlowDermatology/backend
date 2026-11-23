"""The two OTP delivery channels (customer contract §2.1.1): SMS to a phone number and e-mail to an address,
and the purposes that only one of them may carry.
"""

from __future__ import annotations

from glowderma.core import enums

SMS_CHANNEL = "sms"
EMAIL_CHANNEL = "email"
# Purposes bound to one channel: attaching a phone is proven by SMS, verifying an e-mail by e-mail.
CHANNEL_ONLY_PURPOSES: dict[str, str] = {
    enums.OtpPurpose.ATTACH_PHONE: SMS_CHANNEL,
    enums.OtpPurpose.VERIFY_EMAIL: EMAIL_CHANNEL,
}


__all__ = ["SMS_CHANNEL", "EMAIL_CHANNEL", "CHANNEL_ONLY_PURPOSES"]
