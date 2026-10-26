"""The FCM port (design 03 §3.4–§3.6, customer contract §2.14.2): one batch call, one result per message,
never an exception per token.

Compliance C1: a push carries no name, phone, address or amount, only the rendered title/body and a small
``data`` map of ids (all values strings). ``data_only`` messages (``session_replaced``) have no notification
block and go out as APNs background pushes (``content-available``, push type ``background``, priority 5).
"""

from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import Protocol

# Result errors (customer contract §2.14.2):
# the token is dead (404 UNREGISTERED, SENDER_ID_MISMATCH, INVALID_ARGUMENT naming the token) → prune the
# device;
UNREGISTERED = "unregistered"
# any other INVALID_ARGUMENT (a malformed message) → the message failed, the device is never touched.
PAYLOAD_INVALID = "payload_invalid"
# the message was provably **not** delivered (never sent: the batch stopped or ran out of time; or FCM
# answered 429 / 5xx / UNAVAILABLE / INTERNAL / QUOTA_EXCEEDED, or refused our credentials) → the caller gives
# the claim back and sends it again later (never "failed").
NOT_SENT = "not_sent"
# the request may have reached FCM but no answer came back → the outcome is unknown: failed, never resent
# (a blind resend could push twice).
UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PushMessage:
    token: str
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)
    _: KW_ONLY
    data_only: bool = False
    android_channel: str = ""
    android_priority: str = "high"
    apns_push_type: str = "alert"
    apns_priority: int = 10
    image_url: str = ""
    collapse_key: str = ""

    @classmethod
    def data_message(cls, token: str, data: dict[str, str], *, collapse_key: str = "") -> PushMessage:
        """A data-only message (no notification block): APNs background push, priority 5."""
        return cls(
            token,
            "",
            "",
            data,
            data_only=True,
            android_priority="high",
            apns_push_type="background",
            apns_priority=5,
            collapse_key=collapse_key,
        )


@dataclass(frozen=True, slots=True)
class PushResult:
    """``ok`` with the provider ``message_id``, or ``error`` (``unregistered``, ``payload_invalid``, …)."""

    ok: bool
    message_id: str = ""
    error: str = ""


class FcmUnavailable(Exception):
    """Nothing of the batch went out (every result would be ``not_sent``: FCM unreachable, overloaded or
    refusing our credentials): the caller gives every claim back and the outbox retries the batch later."""


class FcmPort(Protocol):
    name: str

    def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        """One result per message, in order: ``ok``, or ``error`` ``unregistered`` / ``payload_invalid`` /
        ``not_sent`` (give the claim back, send later) / ``unavailable`` (unknown: never resent) / another
        adapter code. Raises :class:`FcmUnavailable` only when no message went out at all."""
        ...
