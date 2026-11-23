"""Reads of customer sign-in sessions (B-W1: customer contract §2.2.10, admin §2.6): whether a session is
open,
the open sessions of a customer and the row the "Faol seanslar" list shows for each one."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db.models import QuerySet

from glowderma.accounts.models import AuthSession, Device
from glowderma.core import enums, i18n, timeutils, useragent

# The location the list shows when nothing is known (no GeoIP database, a private or unknown address).
UNKNOWN_LOCATION = "—"
# The app's own name in a device label ("Glow Derma iOS (iPhone 15)").
APP_NAME = "Glow Derma"
APP_PLATFORMS: dict[str, str] = {
    enums.OrderChannel.IOS.value: "iOS",
    enums.OrderChannel.ANDROID.value: "Android",
}


@dataclass(frozen=True, slots=True)
class SessionView:
    """One row of the session list."""

    id: str
    device: str
    platform: str  # OrderChannel literal; "" when the session has no device row
    location: str
    ip: str | None
    created_at: datetime
    updated_at: datetime
    current: bool


def customer_session_open(session_id: str) -> bool:
    """Whether the customer session ``session_id`` is open (not revoked, not past its absolute expiry): a
    loyalty QR token is honoured only while the session that issued it is (B-W1)."""
    try:
        return AuthSession.objects.filter(
            pk=session_id,
            audience="customer",
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        ).exists()
    except ValidationError:  # not a UUID
        return False


def open_sessions(customer_id: int) -> QuerySet[AuthSession]:
    """The customer's open sessions (not revoked, not past the absolute expiry), most recently active first
    (``last_used_at`` desc, then id: a total order, so pages never repeat or skip a row)."""
    return (
        AuthSession.objects.filter(
            audience="customer",
            customer_id=customer_id,
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        )
        .select_related("device")
        .order_by("-last_used_at", "id")
    )


def device_label(session: AuthSession, language: str) -> str:
    """What the list calls the session's client: ``"Chrome (Windows)"`` for a browser (from its user agent),
    ``"Glow Derma iOS (iPhone 15)"`` for the app (model from ``PUT me/device/``)."""
    device: Device | None = session.device
    platform = device.platform if device is not None else ""
    if platform in APP_PLATFORMS:
        name = f"{APP_NAME} {APP_PLATFORMS[platform]}"
        detail = (device.model or device.os) if device is not None else ""
        return f"{name} ({detail})" if detail else name
    unknown = i18n.t("label.session.browser", language)
    if not session.user_agent:
        return unknown
    return useragent.label(session.user_agent, unknown=unknown)


def session_view(session: AuthSession, *, language: str, current_sid: str | None) -> SessionView:
    device: Device | None = session.device
    return SessionView(
        id=str(session.pk),
        device=device_label(session, language),
        platform=device.platform if device is not None else "",
        location=session.location or UNKNOWN_LOCATION,
        ip=session.last_ip,
        created_at=session.created_at,
        updated_at=max(session.last_used_at, session.created_at),
        current=current_sid is not None and str(session.pk) == str(current_sid),
    )


__all__ = [
    "APP_NAME",
    "APP_PLATFORMS",
    "UNKNOWN_LOCATION",
    "SessionView",
    "customer_session_open",
    "device_label",
    "open_sessions",
    "session_view",
]
