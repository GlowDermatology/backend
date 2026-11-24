"""The SMS port: one-time codes sent through a provider's universal OTP templates (customer contract §2.14.1,
``docs/integrations/devsms.md``).

The business side says **why** a code is sent (:class:`OtpPurpose`); the adapter picks the provider's
template for it, so no template number leaves the adapter. The text is the provider's approved template:
nothing here renders or sends free text.

The outcomes the ``sms.send`` handler tells apart:

* :class:`SmsDelivery` → accepted (the provider id is stored in ``SmsCode.provider_message_id``);
* :class:`SmsRejected` → refused for good (invalid number, no balance, the service name rejected by
  moderation, universal OTP suspended, …): logged with its ``reason``, never retried;
* :class:`SmsUnavailable` with ``sent=False`` → nothing was delivered and repeating the request is safe (the
  request never left, the provider asked us to come back later, or the request carried the idempotency key
  that makes a repeat return the first answer): the handler un-claims and the outbox retries; with
  ``sent=True`` → the provider charged and may have delivered the code, but cannot say (a repeat only
  returns the same unknown outcome): the claim is kept and nothing is resent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OtpPurpose(StrEnum):
    """Why a code is sent, in the provider-neutral words of the universal OTP templates."""

    ACTION_CONFIRMATION = "action_confirmation"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - a purpose, not a secret
    REGISTRATION = "registration"
    LOGIN = "login"


@dataclass(frozen=True, slots=True)
class OtpSms:
    phone: str  # E.164 "+998901234567"; the adapter sends what the provider expects
    code: str  # the plain code: never logged, never put into an exception
    purpose: OtpPurpose
    sms_code_id: int
    # Stable for every delivery attempt of one code (the outbox may deliver ``sms.send`` more than once), so a
    # repeated request is recognised by the provider and never sends or charges twice.
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SmsDelivery:
    message_id: str
    status: str  # the provider's status at acceptance ("sent", "pending", …)


class SmsRejected(Exception):
    """The provider refused the message for good; ``reason`` is a short machine-readable cause."""

    def __init__(self, detail: str = "", *, reason: str = "rejected") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class SmsUnavailable(Exception):
    """Nothing usable came back: safe to repeat (``sent=False``) or the code may have been delivered
    (``sent=True``, never resent). ``message_id`` is the provider's id of a charged attempt, when it gave
    one (for a later status check)."""

    def __init__(self, detail: str = "", *, sent: bool = False, message_id: str = "") -> None:
        super().__init__(detail or "sms provider unavailable")
        self.sent = sent
        self.message_id = message_id


class SmsPort(Protocol):
    name: str

    def send_otp(self, message: OtpSms) -> SmsDelivery:
        """Send ``message`` through the universal OTP template of its purpose."""
        ...


__all__ = [
    "OtpPurpose",
    "OtpSms",
    "SmsDelivery",
    "SmsPort",
    "SmsRejected",
    "SmsUnavailable",
]
