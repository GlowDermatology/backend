"""Rotating customer refresh tokens (customer contract §2.1.6): rotation, the grace window of a retried
refresh and the revocation of a reused one.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

from glowderma.accounts import tokens
from glowderma.accounts.models import AuthSession
from glowderma.accounts.services.common import hashing, revocation
from glowderma.accounts.services.customer import auth_sessions, devices, locks
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import AuthFailed

logger = logging.getLogger("glowderma.accounts.services")


REFRESH_GRACE_KEY = "auth:refresh_grace:{digest}"
REFRESH_DEAD_KEY = "auth:refresh_dead:{digest}"


@dataclass(frozen=True, slots=True)
class RefreshResult:
    body: dict[str, Any]
    session_id: str


class RefreshUnknown(AuthFailed):
    """No session for the presented token (the view counts the miss in ``refresh_unknown``)."""

    def __init__(self) -> None:
        super().__init__("not_authenticated")


def _grace_key(digest: str) -> str:
    return REFRESH_GRACE_KEY.format(digest=digest)


def _dead_key(digest: str) -> str:
    return REFRESH_DEAD_KEY.format(digest=digest)


def refresh_body(session: AuthSession, raw_refresh: str) -> dict[str, Any]:
    access, expires = tokens.issue_access(session)
    return {
        "access": access,
        "access_expires_at": timeutils.isoformat(expires),
        "refresh": raw_refresh,
        "refresh_expires_at": timeutils.isoformat(session.absolute_expires_at),
    }


def _check_refresh_device(session: AuthSession, fid: str, platform: str | None) -> None:
    device = session.device
    if device is None or device.fid != fid or (platform is not None and device.platform != platform):
        raise AuthFailed("not_authenticated")


def rotate_customer_refresh(
    raw: str, *, fid: str, platform: str | None = None, request: Any = None
) -> RefreshResult:
    """``auth/refresh/`` steps 1–6 (customer contract §2.1.6) in one transaction on the locked row.

    Rotate (current token), grace replay (``auth:refresh_grace:{h}`` exists: identical tokens, nothing
    written, confirmed or not), re-rotation (previous token, rotation unconfirmed and younger than
    ``CUSTOMER_REFRESH_UNCONFIRMED_SECONDS``: the unused current hash is remembered as dead), else reuse → the
    session is revoked ``refresh_reused`` (committed, audited with the request) and 401 ``SESSION_EXPIRED``.
    The grace body is written before commit and deleted again when the transaction rolls back.

    Reuse is decided before the device check: a stale token (previous without a grace body outside the
    re-rotation window, or dead) revokes the session whichever install presents it — one from another
    install is the stronger theft signal, and its presenter gets nothing either way. Only the answers that
    hand out tokens (rotate, grace replay, re-rotation) require the session's device (else 401
    ``not_authenticated``).
    """
    digest = hashing.hash_secret(raw)
    written: list[str] = []
    refusal: Exception | None = None
    reused: tuple[int, Any, bool] | None = None
    try:
        with transaction.atomic():
            session = (
                AuthSession.objects.select_for_update(of=("self",))
                .select_related("device", "customer")
                .filter(audience="customer")
                .filter(Q(refresh_hash=digest) | Q(previous_refresh_hash=digest))
                .first()
            )
            reuse = False
            if session is None:
                dead_sid = cache.get(_dead_key(digest))
                if not dead_sid:
                    raise RefreshUnknown()
                session = (
                    AuthSession.objects.select_for_update(of=("self",))
                    .select_related("device", "customer")
                    .filter(pk=dead_sid, audience="customer")
                    .first()
                )
                if session is None:
                    raise RefreshUnknown()
                reuse = True
            if session.revoked_at is not None:
                revocation.raise_revoked_session(session.revoke_reason)
            now = timeutils.now()
            customer = session.customer
            if (
                session.absolute_expires_at <= now
                or customer is None
                or customer.status != enums.CustomerStatus.ACTIVE
            ):
                raise AuthFailed("SESSION_EXPIRED")
            grace: Any = None
            window = timedelta(seconds=int(settings.CUSTOMER_REFRESH_UNCONFIRMED_SECONDS))
            unconfirmed = (
                not session.rotation_confirmed
                and session.rotated_at is not None
                and now - session.rotated_at <= window
            )
            if not reuse and session.refresh_hash != digest:
                grace = cache.get(_grace_key(digest))
                reuse = not isinstance(grace, dict) and not unconfirmed
            foreign_device = False
            try:
                _check_refresh_device(session, fid, platform)
            except AuthFailed:
                if not reuse:
                    raise
                foreign_device = True
            grace_ttl = int(settings.CUSTOMER_REFRESH_GRACE_SECONDS)
            if not reuse and session.refresh_hash == digest:
                new_raw = secrets.token_urlsafe(32)
                session.previous_refresh_hash = digest
                session.refresh_hash = hashing.hash_secret(new_raw)
                session.rotation += 1
                session.rotation_confirmed = False
                session.rotated_at = session.last_used_at = now
                session.absolute_expires_at = now + timedelta(days=int(settings.CUSTOMER_REFRESH_TTL_DAYS))
                session.save(
                    update_fields=[
                        "previous_refresh_hash",
                        "refresh_hash",
                        "rotation",
                        "rotation_confirmed",
                        "rotated_at",
                        "last_used_at",
                        "absolute_expires_at",
                    ]
                )
                body = refresh_body(session, new_raw)
                cache.set(_grace_key(digest), body, grace_ttl)
                written.append(_grace_key(digest))
                return RefreshResult(body, str(session.pk))
            if not reuse and isinstance(grace, dict):
                return RefreshResult(grace, str(session.pk))
            if not reuse:  # the previous token, no grace body, the rotation unconfirmed: re-rotate
                remaining = max(1, int((session.absolute_expires_at - now).total_seconds()))
                cache.set(_dead_key(session.refresh_hash), str(session.pk), remaining)
                written.append(_dead_key(session.refresh_hash))  # removed again on rollback
                new_raw = secrets.token_urlsafe(32)
                session.refresh_hash = hashing.hash_secret(new_raw)
                session.rotation += 1
                session.rotation_confirmed = False
                session.last_used_at = now
                session.save(update_fields=["refresh_hash", "rotation", "rotation_confirmed", "last_used_at"])
                body = refresh_body(session, new_raw)
                cache.set(_grace_key(digest), body, grace_ttl)
                written.append(_grace_key(digest))
                return RefreshResult(body, str(session.pk))
            # reuse: one of the holders is not the app (from this install or another one). Nothing is
            # written under this session-first lock: the revocation runs below in its own transaction in
            # the customer → session → device order of every other customer write (C§2.7.1).
            reused = (int(customer.pk), session.pk, foreign_device)
            refusal = AuthFailed("SESSION_EXPIRED")
    except Exception:
        for key in written:
            cache.delete(key)
        raise
    if reused is not None:
        _revoke_reused_session(*reused, request=request)
    raise refusal


@transaction.atomic
def _revoke_reused_session(customer_id: int, sid: Any, foreign_device: bool, *, request: Any) -> None:
    """The reuse revocation of ``auth/refresh/`` step 6: under the customer-level lock, the session (if still
    open) → ``refresh_reused`` with its tombstone and **its** QR keys on commit, its device retired when no
    session is left on it (no push to a phone whose token was stolen); audit ``customer.refresh_reused``."""
    customer = locks.lock_customer(customer_id)
    revoked = auth_sessions.revoke_open_sessions(
        AuthSession.objects.filter(pk=sid), enums.SessionRevokeReason.REFRESH_REUSED
    )
    devices.retire_sessionless_devices(customer_id)
    if revoked:
        audit.record(
            action="customer.refresh_reused",
            entity_type="customer",
            entity_id=str(customer_id),
            actor=customer,
            request=request,
            after={"sid": str(sid)},
        )
    logger.warning("customer.refresh_reused sid=%s foreign_device=%s", sid, foreign_device)


__all__ = [
    "REFRESH_GRACE_KEY",
    "REFRESH_DEAD_KEY",
    "RefreshResult",
    "RefreshUnknown",
    "refresh_body",
    "rotate_customer_refresh",
]
