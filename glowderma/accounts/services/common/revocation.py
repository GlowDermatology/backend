"""Session revocation shared by both audiences: the ``auth:sid`` tombstones every worker reads, the single
session revoke, the revocation audit and the customer 401 of a revoked session.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from glowderma.accounts.models import AuthSession
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import AuthFailed, PasswordChanged

logger = logging.getLogger("glowderma.accounts.services")


def tombstone_ttl() -> int:
    """Seconds a ``revoked:<reason>`` tombstone outlives the last access token of either audience."""
    return max(int(settings.STAFF_ACCESS_TTL), int(settings.CUSTOMER_ACCESS_TTL)) + 60


def tombstone_on_commit(sessions: Iterable[AuthSession]) -> None:
    """Write ``revoked:<reason>`` tombstones once the revocation committed.

    Best effort (``robust``): the revocation already committed, so a Redis error must not turn the request
    that made it into a 500 (the client would lose the tokens a sign-in just issued). An unwritten tombstone
    leaves at most the add-only ``auth:sid`` active entry, whose TTL is ≤ 60 s; the failure logs ERROR.
    """
    values = {f"auth:sid:{session.pk}": f"revoked:{session.revoke_reason}" for session in sessions}
    if not values:
        return

    def write() -> None:
        try:
            cache.set_many(values, timeout=tombstone_ttl())
        except Exception:
            logger.exception(
                "auth.tombstone_failed sids=%s", sorted(key.rsplit(":", 1)[-1] for key in values)
            )

    transaction.on_commit(write, robust=True)


@transaction.atomic
def revoke_session(session: AuthSession, reason: str) -> None:
    stored = AuthSession.objects.select_for_update().get(pk=session.pk)
    if stored.revoked_at is None:
        stored.revoked_at = timeutils.now()
        stored.revoke_reason = reason
        stored.save(update_fields=["revoked_at", "revoke_reason"])
    session.revoked_at = stored.revoked_at
    session.revoke_reason = stored.revoke_reason
    tombstone_on_commit([stored])


def audit_revoked_sessions(sessions: Iterable[AuthSession], *, actor: Any, request: Any, reason: str) -> None:
    for session in sessions:
        audit.record(
            action="auth.session_revoked",
            entity_type="auth_session",
            entity_id=str(session.pk),
            actor=actor,
            request=request,
            after={"sid": str(session.pk), "reason": session.revoke_reason},
            reason=reason,
        )


def raise_revoked_session(reason: str) -> None:
    """The 401 of a revoked customer session: ``password_changed`` for that reason, else ``SESSION_EXPIRED``
    (B-W1: a sign-in elsewhere no longer ends a session, so ``SESSION_REPLACED`` is never answered)."""
    if reason == enums.SessionRevokeReason.PASSWORD_CHANGED:
        raise PasswordChanged()
    raise AuthFailed("SESSION_EXPIRED")


__all__ = [
    "tombstone_ttl",
    "tombstone_on_commit",
    "revoke_session",
    "audit_revoked_sessions",
    "raise_revoked_session",
]
