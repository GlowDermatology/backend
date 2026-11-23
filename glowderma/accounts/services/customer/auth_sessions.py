"""Customer sessions (customer contract §1.9, §2.1.6): opening one on a device, signing out, revoking and
logging out. Revocation tombstones and QR revocation run after commit.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.accounts.services.common import hashing, revocation
from glowderma.accounts.services.customer import devices, locks
from glowderma.core import audit, enums, qr_store, timeutils
from glowderma.core.api.exceptions import AuthFailed


@dataclass(frozen=True, slots=True)
class SessionStart:
    """What a successful customer sign-in produced (``start_customer_session``)."""

    session: AuthSession
    raw_refresh: str
    raw_device_key: str | None  # issued only with issue_device_key; returned once in AuthTokens.device_key
    # FCM tokens of the customer's devices this sign-in retired because no open session was left on them
    # (B-W1: after a reset revoked every session; never a device still signed in).
    farewell_tokens: list[str] = field(default_factory=list)


def customer_device_label(platform: str, model: str) -> str:
    """``"<platform> <model or 'app'>"``: the fid never appears in labels."""
    return f"{platform} {model or 'app'}"[:100]


def _revoke_qr_on_commit(customer_id: int) -> None:
    # robust: the data already committed, so a Redis error is logged, never turned into a 500 (the keys
    # expire after QR_TOKEN_TTL_SECONDS anyway).
    transaction.on_commit(lambda: qr_store.revoke_all(customer_id), robust=True)


def _revoke_session_qr_on_commit(sessions: Iterable[AuthSession]) -> None:
    """Delete the QR keys each revoked customer session issued, once the revocation committed (B-W1: a QR
    token is bound to its session; the resolve re-checks the session too, so a lost delete is harmless)."""
    pairs = [(int(session.customer_id), str(session.pk)) for session in sessions if session.customer_id]
    if not pairs:
        return

    def revoke() -> None:
        for customer_id, session_id in pairs:
            qr_store.revoke_session(customer_id, session_id)

    transaction.on_commit(revoke, robust=True)


@transaction.atomic
def start_customer_session(
    customer_id: int,
    fid: str,
    platform: str,
    app_version: str,
    *,
    issue_device_key: bool,
    auth_method: str = AuthSession.AuthMethod.PASSWORD,
) -> SessionStart:
    """Sign a customer in on one device (customer contract §1.9 — B-W1: several sessions at once).

    Under the customer-level lock: (1) upsert ``Device(customer, fid)`` active; (2) an open session **of this
    device** (an earlier sign-in of the same install or browser) → revoked ``replaced`` (tombstone and its QR
    keys on commit) — every other device keeps its session, so the app and any number of browsers stay
    signed in together; (3) devices of the customer left without an open session (a reset revoked them all)
    → inactive, no push, token cleared, the tokens captured into ``farewell_tokens``; (4) with
    ``issue_device_key`` a new device key whose SHA-256 replaces the stored hash; (5) a new session (sliding
    ``CUSTOMER_REFRESH_TTL_DAYS``) and ``Customer.last_login_at``.

    The customer's status is checked under the lock, not only by the caller: a sign-in racing ``DELETE me/``
    (or an admin delete) that commits first is refused with 401 ``SESSION_EXPIRED`` before anything is
    written, so a deleted account never regains a keyed device, an open session or ``last_login_at``
    (customer contract §2.2.3 step 5). The re-registration flow reactivates the customer before calling.
    """
    stored = locks.lock_customer(customer_id)
    if stored.status != enums.CustomerStatus.ACTIVE:
        raise AuthFailed("SESSION_EXPIRED")
    instant = timeutils.now()
    device = Device.objects.select_for_update().filter(customer_id=customer_id, fid=fid).first()
    if device is None:
        device = Device.objects.create(
            customer_id=customer_id,
            fid=fid,
            platform=platform,
            app_version=app_version,
            is_active=True,
            last_active_at=instant,
        )
    else:
        device.is_active = True
        device.platform = platform
        device.app_version = app_version
        device.last_active_at = instant
        if (
            device.fcm_token
            and Device.objects.filter(is_active=True, fcm_token=device.fcm_token)
            .exclude(pk=device.pk)
            .exists()
        ):
            # Another account's active row took this install's token meanwhile; PUT me/device heals it.
            device.fcm_token = ""
            device.push_enabled = False
        device.save(
            update_fields=[
                "is_active",
                "platform",
                "app_version",
                "last_active_at",
                "fcm_token",
                "push_enabled",
                "updated_at",
            ]
        )
    revoke_open_sessions(
        AuthSession.objects.filter(customer_id=customer_id, device_id=device.pk),
        enums.SessionRevokeReason.REPLACED,
        at=instant,
    )
    farewell_tokens = devices.retire_sessionless_devices(customer_id, keep_device_id=device.pk, at=instant)
    raw_device_key: str | None = None
    if issue_device_key:
        raw_device_key = secrets.token_urlsafe(32)
        device.key_hash = hashing.hash_secret(raw_device_key)
        device.save(update_fields=["key_hash", "updated_at"])
    raw_refresh = secrets.token_urlsafe(32)
    session = AuthSession.objects.create(
        audience="customer",
        customer_id=customer_id,
        device=device,
        device_label=customer_device_label(platform, device.model),
        refresh_hash=hashing.hash_secret(raw_refresh),
        created_at=instant,
        last_used_at=instant,
        absolute_expires_at=instant + timedelta(days=int(settings.CUSTOMER_REFRESH_TTL_DAYS)),
        auth_method=auth_method,
    )
    Customer.objects.filter(pk=customer_id).update(last_login_at=instant, updated_at=instant)
    return SessionStart(session, raw_refresh, raw_device_key, farewell_tokens)


def _revoke_customer_sessions(
    customer_id: int, reason: str, *, except_sid: uuid.UUID | str | None = None, at: datetime | None = None
) -> list[AuthSession]:
    queryset = AuthSession.objects.filter(customer_id=customer_id)
    if except_sid is not None:
        queryset = queryset.exclude(pk=except_sid)
    return revoke_open_sessions(queryset, reason, at=at)


def revoke_open_sessions(queryset: Any, reason: str, *, at: datetime | None = None) -> list[AuthSession]:
    """Lock and revoke the open customer sessions of ``queryset`` with ``reason`` (tombstones and the QR keys
    each session issued deleted on commit)."""
    sessions = list(queryset.select_for_update().filter(audience="customer", revoked_at__isnull=True))
    instant = at or timeutils.now()
    for session in sessions:
        session.revoked_at = instant
        session.revoke_reason = reason
    if sessions:
        AuthSession.objects.filter(pk__in=[session.pk for session in sessions]).update(
            revoked_at=instant, revoke_reason=reason
        )
    revocation.tombstone_on_commit(sessions)
    _revoke_session_qr_on_commit(sessions)
    return sessions


def sign_out_sessions(
    customer_id: int, reason: str, *, except_sid: uuid.UUID | str | None, keep_fid: str | None = None
) -> tuple[list[AuthSession], list[str]]:
    """:func:`sign_out_customer` returning the revoked sessions too."""
    if reason not in enums.SessionRevokeReason.values:
        raise ValueError(f"Unknown revoke reason {reason!r}")
    instant = timeutils.now()
    sessions = _revoke_customer_sessions(customer_id, reason, except_sid=except_sid, at=instant)
    keep = None
    if except_sid is None:
        _revoke_qr_on_commit(customer_id)
    else:
        keep = AuthSession.objects.filter(pk=except_sid).values_list("device_id", flat=True).first()
    if keep is None and keep_fid is not None:
        keep = (
            Device.objects.filter(customer_id=customer_id, fid=keep_fid).values_list("pk", flat=True).first()
        )
    return sessions, devices.retire_sessionless_devices(customer_id, keep_device_id=keep, at=instant)


def revoke_session_rows(queryset: Any, reason: str) -> list[AuthSession]:
    """Revoke the open customer sessions of ``queryset`` (tombstones and their QR keys on commit); the
    caller holds the customer-level lock and retires the devices left without a session."""
    return revoke_open_sessions(queryset, reason)


def sign_out_customer(
    customer_id: int,
    reason: str,
    *,
    except_sid: uuid.UUID | str | None = None,
    keep_fid: str | None = None,
) -> list[str]:
    """Revoke every open session of the customer (but ``except_sid``) with ``reason`` and retire the devices
    left without one (B-W1) — except ``except_sid``'s device, or the device ``keep_fid`` (the install a reset
    runs on, which signs in again at once and keeps its push token). Returns the retired devices' farewell
    tokens, so a security notice reaches the phones it signed out (customer contract §2.1.5, §2.2.9). Runs
    inside the caller's transaction, under its customer-level lock."""
    return sign_out_sessions(customer_id, reason, except_sid=except_sid, keep_fid=keep_fid)[1]


@transaction.atomic
def revoke_customer_sessions(customer_id: int, reason: str, except_sid: uuid.UUID | str | None = None) -> int:
    """Revoke every open session of the customer (but ``except_sid``) with ``reason``.

    Tombstones ``revoked:<reason>`` are written on commit (never a plain delete) with the tombstone TTL, and
    the QR keys of the revoked sessions (every key of the customer without ``except_sid``) are deleted on
    commit. The devices left without an open session are retired (no push). Returns the number of sessions
    revoked.
    """
    return len(sign_out_sessions(customer_id, reason, except_sid=except_sid)[0])


@transaction.atomic
def logout_customer(raw: str, *, scope: str = enums.LogoutScope.CURRENT, request: Any = None) -> AuthSession:
    """``auth/logout/`` (customer contract §2.1.7): revoke the session owning the refresh token (``logout``,
    tombstone and its QR keys on commit) and retire its device (inactive, no push, no token; its key hash
    kept); audit ``customer.logout {sid, scope}``. ``scope`` (B-W1): ``current`` (default) only this session,
    ``others`` every other session of the customer (this one stays), ``all`` every session ("log out
    everywhere"); the devices left without an open session are retired too. ``others`` / ``all`` need the
    session's **current** refresh token (a previous one ends only its own session). Unknown, revoked or
    expired → 401 ``SESSION_EXPIRED``.

    Lock order Customer → session → device (C§2.7.1), as every other customer write: the session is first
    resolved without a lock, the customer-level lock taken, then the session re-read ``FOR UPDATE`` and
    re-checked (a sign-in, ``DELETE me/`` or ``PUT me/device/`` racing the logout serializes instead of
    deadlocking with it).
    """
    if scope not in enums.LogoutScope.values:
        raise ValueError(f"Unknown logout scope {scope!r}")
    digest = hashing.hash_secret(raw)
    match = Q(refresh_hash=digest) | Q(previous_refresh_hash=digest)
    customer_id = (
        AuthSession.objects.filter(audience="customer")
        .filter(match)
        .values_list("customer_id", flat=True)
        .first()
    )
    if customer_id is None:
        raise AuthFailed("SESSION_EXPIRED")
    locks.lock_customer(customer_id)
    session = (
        AuthSession.objects.select_for_update(of=("self",))
        .select_related("customer")
        .filter(audience="customer", customer_id=customer_id)
        .filter(match)
        .first()
    )
    if session is None or session.revoked_at is not None or session.absolute_expires_at <= timeutils.now():
        raise AuthFailed("SESSION_EXPIRED")
    if scope != enums.LogoutScope.CURRENT and session.refresh_hash != digest:
        # signing other devices out needs the current refresh token: a previous one (still accepted to end
        # its own session during the rotation grace) may be a stolen copy
        raise AuthFailed("SESSION_EXPIRED")
    reason = enums.SessionRevokeReason.LOGOUT
    if scope == enums.LogoutScope.CURRENT:
        revoke_open_sessions(AuthSession.objects.filter(pk=session.pk), reason)
        devices.retire_sessionless_devices(customer_id)
    else:
        sign_out_sessions(
            customer_id, reason, except_sid=session.pk if scope == enums.LogoutScope.OTHERS else None
        )
    audit.record(
        action="customer.logout",
        entity_type="customer",
        entity_id=str(customer_id),
        actor=session.customer,
        request=request,
        after={"sid": str(session.pk), "scope": scope},
    )
    return session


__all__ = [
    "SessionStart",
    "customer_device_label",
    "start_customer_session",
    "revoke_open_sessions",
    "sign_out_sessions",
    "revoke_session_rows",
    "sign_out_customer",
    "revoke_customer_sessions",
    "logout_customer",
]
