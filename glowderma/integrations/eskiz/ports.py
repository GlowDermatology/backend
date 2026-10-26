"""The Eskiz SMS port (customer contract §2.14.1).

One call per message. The outcomes the ``sms.send`` handler needs to tell apart:

* a message id → accepted (stored in ``SmsCode.provider_message_id``);
* :class:`EskizRejected` → a definitive 4xx (invalid number, no balance): logged, never retried;
* :class:`EskizUnavailable` with ``sent=False`` → the request never left (connect failure): the handler
  un-claims and the outbox retries; with ``sent=True`` → a timeout or 5xx after sending: the claim is kept and
  the message is never resent (the user may already have the code).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SmsMessage:
    phone: str  # E.164 "+998901234567"; the adapter sends the digits without "+"
    text: str
    sms_code_id: int


class EskizRejected(Exception):
    """Eskiz refused the message for good (4xx)."""


class EskizUnavailable(Exception):
    """Eskiz could not be reached (``sent=False``) or did not answer after the request left
    (``sent=True``)."""

    def __init__(self, detail: str = "", *, sent: bool = False) -> None:
        super().__init__(detail or "eskiz unavailable")
        self.sent = sent


class EskizPort(Protocol):
    def send(self, message: SmsMessage) -> str:
        """Send ``message``; returns the provider message id."""
        ...
