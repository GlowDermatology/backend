"""Staff sessions (admin contract §2.1): opening one under the staff row lock, the refresh, the validity
check every authenticated request makes and the revocation of a member's sessions.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.services.common import revocation
from glowderma.core import timeutils
from glowderma.core.api.exceptions import AuthFailed


@transaction.atomic
def start_staff_session(
    user: StaffUser,
    *,
    device_label: str,
    auth_method: str = AuthSession.AuthMethod.PASSWORD,
    failure_code: str = "invalid_credentials",
) -> tuple[AuthSession, str]:
    """Open a staff session; ``auth_method`` records how the sign-in was proven (``password`` for email +
    password, the provider for a social sign-in): only a password session may link an identity (§2.1).

    The session is created under the staff row lock that :func:`reset_staff_password` and
    :func:`deactivate_staff` also take, and the proof is checked again under it: the account must still be
    active and, for a password sign-in, its stored hash must still be the one ``user`` was authenticated
    with. A sign-in that raced a reset or a deactivation therefore never opens a session the revocation
    missed; it fails with ``failure_code`` (401) instead.
    """
    if auth_method not in AuthSession.AuthMethod.values:
        raise ValueError(f"Unknown auth method {auth_method!r}")
    locked = StaffUser.objects.select_for_update().filter(pk=user.pk).only("password", "is_active").first()
    if (
        locked is None
        or not locked.is_active
        or (auth_method == AuthSession.AuthMethod.PASSWORD and locked.password != user.password)
    ):
        raise AuthFailed(failure_code)
    refresh = secrets.token_urlsafe(32)
    session = AuthSession.objects.create(
        audience="staff",
        staff=user,
        device_label=device_label[:100],
        refresh_hash=hashlib.sha256(refresh.encode()).hexdigest(),
        absolute_expires_at=timeutils.now() + timedelta(seconds=settings.STAFF_REFRESH_TTL),
        auth_method=auth_method,
    )
    return session, refresh


def check_session(session: AuthSession) -> None:
    if session.revoked_at is not None:
        raise AuthFailed("SESSION_REPLACED" if session.revoke_reason == "replaced" else "SESSION_EXPIRED")
    if session.absolute_expires_at <= timeutils.now() or session.staff is None or not session.staff.is_active:
        raise AuthFailed("SESSION_EXPIRED")


@transaction.atomic
def refresh_staff_session(raw_refresh: str) -> AuthSession:
    session = (
        AuthSession.objects.select_for_update(of=("self",))
        .select_related("staff")
        .filter(refresh_hash=hashlib.sha256(raw_refresh.encode()).hexdigest(), audience="staff")
        .first()
    )
    if session is None:
        raise AuthFailed("not_authenticated")
    check_session(session)
    session.last_used_at = timeutils.now()
    session.save(update_fields=["last_used_at"])
    return session


@transaction.atomic
def revoke_user_sessions(user: StaffUser, reason: str, *, except_sid: uuid.UUID | None = None) -> int:
    return len(revoke_user_sessions_locked(user, reason, except_sid=except_sid))


def revoke_user_sessions_locked(
    user: StaffUser, reason: str, *, except_sid: uuid.UUID | None = None
) -> list[AuthSession]:
    queryset = AuthSession.objects.select_for_update().filter(staff=user, revoked_at__isnull=True)
    if except_sid is not None:
        queryset = queryset.exclude(pk=except_sid)
    sessions = list(queryset)
    instant = timeutils.now()
    for session in sessions:
        session.revoked_at = instant
        session.revoke_reason = reason
    if sessions:
        AuthSession.objects.filter(pk__in=[session.pk for session in sessions]).update(
            revoked_at=instant, revoke_reason=reason
        )
    revocation.tombstone_on_commit(sessions)
    return sessions


__all__ = [
    "start_staff_session",
    "check_session",
    "refresh_staff_session",
    "revoke_user_sessions",
    "revoke_user_sessions_locked",
]
