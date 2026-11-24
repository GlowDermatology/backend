"""The signed, single-use social ticket that carries a social sign-in to its phone completion."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core import signing
from django.core.cache import cache

from glowderma.core import enums
from glowderma.core.api.exceptions import (
    SocialTicketInvalid,
)
from glowderma.core.pii import pii_ref

SOCIAL_TICKET_SALT = "gd.social"


SOCIAL_APPLE_KEY = "gd:social:apple:{jti}"


SOCIAL_TICKET_USED_KEY = "gd:social:ticket:{jti}"


@dataclass(frozen=True, slots=True)
class SocialTicket:
    provider: str
    subject: str
    email: str
    jti: str
    trusted: bool = False


def read_social_ticket(ticket: str, *, fid: str) -> SocialTicket:
    """A valid ticket (signature, ≤ ``SOCIAL_TICKET_TTL_SECONDS``, unused, issued to this install), else 400
    ``social_ticket_invalid`` (C§2.1.1 row 4, §2.1.9). "Unused" is the ``gd:social:ticket:{jti}`` marker a
    completion claims; :func:`complete_social` still claims it atomically (``cache.add``)."""
    try:
        data = signing.TimestampSigner(salt=SOCIAL_TICKET_SALT).unsign_object(
            ticket, max_age=int(settings.SOCIAL_TICKET_TTL_SECONDS)
        )
    except signing.BadSignature as exc:
        raise SocialTicketInvalid() from exc
    if (
        not isinstance(data, dict)
        or data.get("fid") != pii_ref(fid)
        or data.get("provider") not in enums.CustomerSocialProvider.values
    ):
        raise SocialTicketInvalid()
    jti = str(data.get("jti") or "")
    if not jti or cache.get(SOCIAL_TICKET_USED_KEY.format(jti=jti)) is not None:
        raise SocialTicketInvalid()
    return SocialTicket(
        str(data["provider"]),
        str(data["subject"]),
        str(data.get("email") or ""),
        jti,
        trusted=data.get("trusted") is True,
    )


__all__ = [
    "SOCIAL_APPLE_KEY",
    "SOCIAL_TICKET_SALT",
    "SOCIAL_TICKET_USED_KEY",
    "SocialTicket",
    "read_social_ticket",
]
