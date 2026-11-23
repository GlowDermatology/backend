"""What a request says about the end user's client: user agent and IP (``core.clientip``: the website's BFF
forwards them as ``X-GD-Client-UA`` / ``X-GD-Client-IP``, honoured only with the internal secret)."""

from __future__ import annotations

from typing import Any

from glowderma.core import enums
from glowderma.core.clientip import client_user_agent, forwarded_client_ip


def client_facts(request: Any) -> tuple[str, str]:
    """``(user_agent, ip)`` of the end user behind ``request`` (``core.clientip``: the BFF's
    ``X-GD-Client-UA`` / ``X-GD-Client-IP`` count only with the internal secret)."""
    django_request = getattr(request, "_request", request)
    ip = str(getattr(django_request, "client_ip", "") or django_request.META.get("REMOTE_ADDR", ""))
    return client_user_agent(django_request), ip


def activity_facts(request: Any, *, platform: str) -> tuple[str, str]:
    """What the activity bump may record about the client: the app's own ``User-Agent`` and peer IP; for
    the website only what its BFF forwarded (``X-GD-Client-UA`` / ``X-GD-Client-IP`` with the internal
    secret) — never the BFF server's own user agent or IP. ``""`` = leave the stored value."""
    django_request = getattr(request, "_request", request)
    if platform != enums.OrderChannel.WEB:
        return client_facts(request)
    forwarded_ip = forwarded_client_ip(django_request) or ""
    forwarded_ua = ""
    if forwarded_ip:
        forwarded_ua = (
            client_user_agent(django_request) if django_request.META.get("HTTP_X_GD_CLIENT_UA") else ""
        )
    return forwarded_ua, forwarded_ip


__all__ = [
    "activity_facts",
    "client_facts",
]
