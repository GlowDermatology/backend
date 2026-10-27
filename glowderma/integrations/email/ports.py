"""The e-mail port (hybrid identity, customer contract §2.1, §2.14.4): one call per message.

The outcomes the ``email.send`` handler tells apart (as the SMS port's, ``integrations.eskiz.ports``):

* a message id → accepted (stored in ``SmsCode.provider_message_id``);
* :class:`EmailRejected` → a definitive refusal (the relay refused the recipient or the sender): logged,
  never retried;
* :class:`EmailUnavailable` with ``sent=False`` → nothing left the process or the relay accepted nothing (no
  connection, refused login): the handler un-claims and the outbox retries; with ``sent=True`` → the outcome
  is unknown (a timeout or a broken connection once the message may have been handed over): the claim is
  kept and the message is never resent (the user may already have the code).

No adapter logs an address, a subject or a body: log lines carry the message ``reference`` only (C§6.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str  # one recipient, already normalised (lower case)
    subject: str
    text: str  # the plain-text part
    html: str  # the HTML alternative
    reference: str  # what log lines may name, e.g. "sms_code_id=12"


class EmailRejected(Exception):
    """The relay refused the message for good (recipient or sender refused, a permanent 5xx)."""


class EmailUnavailable(Exception):
    """The relay could not be reached or refused us before accepting anything (``sent=False``), or the
    outcome is unknown once the message may have left (``sent=True``)."""

    def __init__(self, detail: str = "", *, sent: bool = False) -> None:
        super().__init__(detail or "email unavailable")
        self.sent = sent


class EmailPort(Protocol):
    def send(self, message: EmailMessage) -> str:
        """Send ``message``; returns the message id."""
        ...
