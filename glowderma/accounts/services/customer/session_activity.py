"""What the customer's session list records about each client: the stamp of a just-opened session and the
throttled activity bump of a signed-in request (B-W1, customer contract §2.2.10)."""

from __future__ import annotations

import uuid
from typing import Any

from glowderma.accounts.models import AuthSession
from glowderma.core import geoip, timeutils
from glowderma.core.clientip import USER_AGENT_MAX


def stamp_session(session_id: uuid.UUID | str, *, user_agent: str, ip: str) -> None:
    """Record the client of a just-opened session (every customer sign-in): user agent, IP and location."""
    AuthSession.objects.filter(pk=session_id, audience="customer").update(
        user_agent=user_agent[:USER_AGENT_MAX],
        last_ip=ip or None,
        location=geoip.locate(ip) if ip else "",
    )


def touch_session(session_id: uuid.UUID | str, *, user_agent: str, ip: str) -> None:
    """The throttled activity bump of a signed-in request (``CustomerJWTAuthentication``, at most every
    ``DEVICE_BUMP_SECONDS`` per session): ``last_used_at`` = now; the user agent when it changed; the IP and
    its location only when the IP changed (one GeoIP read per change, never per request)."""
    instant = timeutils.now()
    row = AuthSession.objects.filter(pk=session_id).values("last_ip", "user_agent").first()
    if row is None:
        return
    changes: dict[str, Any] = {"last_used_at": instant}
    if user_agent and user_agent[:USER_AGENT_MAX] != row["user_agent"]:
        changes["user_agent"] = user_agent[:USER_AGENT_MAX]
    if ip and ip != (row["last_ip"] or ""):
        changes["last_ip"] = ip
        changes["location"] = geoip.locate(ip)
    AuthSession.objects.filter(pk=session_id).update(**changes)


__all__ = [
    "stamp_session",
    "touch_session",
]
