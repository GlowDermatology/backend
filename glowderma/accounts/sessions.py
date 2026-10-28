"""Customer session management (B-W1, customer contract §2.2.10, admin contract §2.6).

A customer may be signed in on any number of phones and browsers at once (§1.9). This module keeps what the
"Faol seanslar" list shows about each session — the client's user agent, its last IP and the place a local
GeoIP City database resolves it to — and revokes sessions one by one or all but the current one, for the
customer and for staff. Writes follow the customer lock order of §2.7.1 (Customer → session → device).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import QuerySet

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.core import audit, enums, geoip, i18n, timeutils, useragent
from glowderma.core.clientip import USER_AGENT_MAX, client_user_agent, forwarded_client_ip

# The location the list shows when nothing is known (no GeoIP database, a private or unknown address).
UNKNOWN_LOCATION = "—"
# The app's own name in a device label ("Glow Derma iOS (iPhone 15)").
APP_NAME = "Glow Derma"
APP_PLATFORMS: dict[str, str] = {
    enums.OrderChannel.IOS.value: "iOS",
    enums.OrderChannel.ANDROID.value: "Android",
}


class SessionNotFound(Exception):
    """No open session with that id belongs to the customer (the API answers 404)."""


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


def _locked_open_session(customer_id: int, session_id: str) -> AuthSession:
    try:
        uuid.UUID(str(session_id))
    except ValueError as exc:
        raise SessionNotFound from exc
    session = (
        AuthSession.objects.select_for_update(no_key=True)
        .filter(
            pk=session_id,
            audience="customer",
            customer_id=customer_id,
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        )
        .first()
    )
    if session is None:
        raise SessionNotFound
    return session


@transaction.atomic
def revoke_session(
    customer_id: int,
    session_id: str,
    *,
    actor: Any,
    actor_type: str,
    request: Any = None,
    current_sid: str | None = None,
) -> AuthSession:
    """Revoke one open session of the customer — the row menu "Chiqish" (customer: reason ``logout``) or the
    admin customer card (staff: reason ``admin_revoked``). Its tombstone and QR keys go on commit, its device
    is retired when no other session is left on it (no push). The customer's own current session may be
    revoked too (it is a logout). Audit ``customer.session_revoked {sid, current}``."""
    if actor_type == "customer" and current_sid:
        # the caller's own session must still be open under the lock (a concurrent revocation wins)
        services.lock_signed_in_customer(customer_id, current_sid)
    else:
        services.lock_customer(customer_id)
    session = _locked_open_session(customer_id, session_id)
    reason = (
        enums.SessionRevokeReason.LOGOUT
        if actor_type == "customer"
        else enums.SessionRevokeReason.ADMIN_REVOKED
    )
    services.revoke_session_rows(AuthSession.objects.filter(pk=session.pk), reason)
    services.retire_sessionless_devices(customer_id)
    audit.record(
        action="customer.session_revoked",
        entity_type="customer",
        entity_id=str(customer_id),
        actor=actor,
        actor_type=actor_type,
        request=request,
        after={"sid": str(session.pk), "current": str(session.pk) == str(current_sid or "")},
    )
    return session


@transaction.atomic
def revoke_other_sessions(customer_id: int, current_sid: str, *, actor: Customer, request: Any = None) -> int:
    """ "Boshqa qurilmalardan chiqish": every open session of the customer but ``current_sid`` → ``logout``;
    the devices left without one are retired. Re-checks under the customer lock that the caller's own
    session is still open (401 otherwise). Audit ``customer.sessions_revoked {count, scope: "others"}``.
    Returns how many sessions ended."""
    services.lock_signed_in_customer(customer_id, current_sid)
    sessions, _farewell = services.sign_out_sessions(
        customer_id, enums.SessionRevokeReason.LOGOUT, except_sid=current_sid
    )
    audit.record(
        action="customer.sessions_revoked",
        entity_type="customer",
        entity_id=str(customer_id),
        actor=actor,
        actor_type="customer",
        request=request,
        after={"count": len(sessions), "scope": enums.LogoutScope.OTHERS},
    )
    return len(sessions)
