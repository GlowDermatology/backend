"""The sign-in challenge of the admin panel MFA: issued after a correct password or provider step, kept in the
cache, answered with a code at most :data:`policy.CHALLENGE_MAX_ATTEMPTS` times and used up once."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.core.cache import cache

from glowderma.accounts.mfa import audit_trail, policy, state
from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils


@dataclass(frozen=True)
class Challenge:
    raw: str
    expires_at: datetime


class MfaRequired(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised by ``services.social_login`` instead of opening a session: the view answers the challenge.
    Anything else that lets it escape fails closed (500, no session)."""

    def __init__(self, challenge: Challenge) -> None:
        super().__init__("mfa_required")
        self.challenge = challenge


@dataclass(frozen=True)
class PendingSignIn:
    digest: str
    staff_id: int
    method: str
    identity_id: int | None
    marker: str


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _key(digest: str) -> str:
    return f"auth:mfa:challenge:{digest}"


def _attempts_key(digest: str) -> str:
    return f"auth:mfa:attempts:{digest}"


def _used_key(digest: str) -> str:
    return f"auth:mfa:used:{digest}"


def issue_challenge(
    user: StaffUser, method: str, *, identity_id: int | None = None, request: Any = None
) -> Challenge:
    """A sign-in challenge for ``user`` after a correct ``method`` (``password`` or a provider) step."""
    raw = secrets.token_urlsafe(32)
    expires_at = timeutils.now() + timedelta(seconds=policy.CHALLENGE_TTL_SECONDS)
    cache.set(
        _key(_digest(raw)),
        {
            "uid": user.pk,
            "method": method,
            "identity": identity_id,
            "marker": user.get_session_auth_hash(),
            "exp": expires_at.timestamp(),
        },
        timeout=policy.CHALLENGE_TTL_SECONDS,
    )
    audit_trail.record_mfa_event("auth.mfa_challenged", user, request, email=user.email, method=method)
    return Challenge(raw, expires_at)


def open_challenge(raw: object) -> PendingSignIn | None:
    """The pending sign-in of ``raw``, or ``None`` (malformed, unknown, expired)."""
    if not isinstance(raw, str) or not raw or len(raw) > policy.CHALLENGE_MAX_LENGTH:
        return None
    digest = _digest(raw)
    data = cache.get(_key(digest))
    if not isinstance(data, dict) or float(data.get("exp", 0)) <= timeutils.now().timestamp():
        return None
    identity = data.get("identity")
    return PendingSignIn(
        digest=digest,
        staff_id=int(data["uid"]),
        method=str(data["method"]),
        identity_id=int(identity) if identity is not None else None,
        marker=str(data["marker"]),
    )


def drop_challenge(pending: PendingSignIn) -> None:
    cache.delete_many([_key(pending.digest), _attempts_key(pending.digest)])


def count_attempt(pending: PendingSignIn) -> bool:
    """Count one code attempt against the challenge: ``False`` (and the challenge dropped) beyond
    :data:`CHALLENGE_MAX_ATTEMPTS`. Atomic across workers (``add`` + ``incr``)."""
    key = _attempts_key(pending.digest)
    cache.add(key, 0, timeout=policy.CHALLENGE_TTL_SECONDS)
    try:
        count = int(cache.incr(key))
    except ValueError:  # expired between the two calls: the challenge is gone with it
        count = policy.CHALLENGE_MAX_ATTEMPTS + 1
    if count > policy.CHALLENGE_MAX_ATTEMPTS:
        drop_challenge(pending)
        return False
    return True


def claim_challenge(pending: PendingSignIn) -> bool:
    """Use the challenge up (once, across workers); ``False`` when another request already did."""
    claimed = bool(cache.add(_used_key(pending.digest), 1, timeout=policy.CHALLENGE_TTL_SECONDS))
    drop_challenge(pending)
    return claimed


def challenge_matches(pending: PendingSignIn, user: StaffUser | None) -> bool:
    """The challenge still belongs to an active account whose password and panel MFA are unchanged."""
    return (
        user is not None
        and user.pk == pending.staff_id
        and user.is_active
        and secrets.compare_digest(user.get_session_auth_hash(), pending.marker)
        and state.enabled(user)
    )


__all__ = [
    "Challenge",
    "MfaRequired",
    "PendingSignIn",
    "challenge_matches",
    "claim_challenge",
    "count_attempt",
    "drop_challenge",
    "issue_challenge",
    "open_challenge",
]
