"""Outbox topics produced by ``accounts`` and the customer flows (customer contract §1.9, §2.1, §2.14.2).

Producers (P1) and handlers (``accounts.tasks`` here, ``notifications.tasks`` in P3) import these names and
builders; nobody writes a topic literal, a dedupe key or the ``push.account_security`` payload by hand.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, Final

from glowderma.core import enums

SMS_SEND: Final = "sms.send"
EMAIL_SEND: Final = "email.send"
EMAIL_NOTICE: Final = "email.notice"
PUSH_SESSION_REPLACED: Final = "push.session_replaced"
PUSH_ACCOUNT_SECURITY: Final = "push.account_security"
APPLE_REVOKE: Final = "apple.revoke"

# Customer contract §2.14.2: the one definition of the push.account_security payload. Every key is always
# present.
ACCOUNT_SECURITY_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "customer_id",
    "event",
    "event_id",
    "provider",
    "farewell_tokens",
)


def sms_send_key(sms_code_id: int) -> str:
    return f"{SMS_SEND}:{sms_code_id}"


def email_send_key(sms_code_id: int) -> str:
    return f"{EMAIL_SEND}:{sms_code_id}"


def session_replaced_key(session_id: Any) -> str:
    return f"{PUSH_SESSION_REPLACED}:{session_id}"


def account_security_key(customer_id: int, event: str, event_id: str) -> str:
    return f"{PUSH_ACCOUNT_SECURITY}:{customer_id}:{event}:{event_id}"


def apple_revoke_key(identity_id: int) -> str:
    return f"{APPLE_REVOKE}:{identity_id}"


def account_security_payload(
    customer_id: int, event: str, *, provider: str = "", farewell_tokens: Iterable[str] = ()
) -> dict[str, Any]:
    """The ``push.account_security`` payload with a fresh uuid4-hex ``event_id``.

    ``event`` is an ``AccountSecurityEvent``; ``provider`` a ``CustomerSocialProvider`` for the social events,
    else ``""``; ``farewell_tokens`` the FCM tokens a reset or a social completion took from the devices it
    deactivated (``SessionStart.farewell_tokens``), else empty.
    """
    if event not in enums.AccountSecurityEvent.values:
        raise ValueError(f"Unknown account security event {event!r}")
    social = event in (enums.AccountSecurityEvent.SOCIAL_LINKED, enums.AccountSecurityEvent.SOCIAL_UNLINKED)
    if social != bool(provider) or (provider and provider not in enums.CustomerSocialProvider.values):
        raise ValueError("provider is required for the social events and only for them")
    tokens = [str(token) for token in farewell_tokens if token]
    values = (int(customer_id), str(event), uuid.uuid4().hex, str(provider), tokens)
    return dict(zip(ACCOUNT_SECURITY_PAYLOAD_KEYS, values, strict=True))


def account_security_inbox_key(customer_id: int, event: str, event_id: str) -> str:
    """The inbox ``dedupe_key`` of an account-security notice (customer contract §2.12)."""
    return f"account:{customer_id}:{event}:{event_id}"
