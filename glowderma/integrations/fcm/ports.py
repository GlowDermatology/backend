"""The FCM port (design 03 §3.4–§3.6): one batch call, one result per message, never an exception per token.

Compliance C1: a push carries no name or phone, only the rendered title/body and a small ``data`` map of ids.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

# FCM's error code for a token that is no longer registered (the device reinstalled or revoked the app).
UNREGISTERED = "unregistered"


@dataclass(frozen=True, slots=True)
class PushMessage:
    token: str
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PushResult:
    """``ok`` with the provider ``message_id``, or ``error`` (an FCM error code such as ``unregistered``)."""

    ok: bool
    message_id: str = ""
    error: str = ""


class FcmUnavailable(Exception):
    """The provider did not answer (transport error): the caller retries the whole batch later."""


class FcmPort(Protocol):
    name: str

    def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        """One result per message, in order. Raises :class:`FcmUnavailable` only for transport failures."""
        ...
