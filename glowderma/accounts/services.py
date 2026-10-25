import hashlib
import secrets
import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import AuthFailed


def start_staff_session(user: StaffUser, *, device_label: str) -> tuple[AuthSession, str]:
    refresh = secrets.token_urlsafe(32)
    session = AuthSession.objects.create(
        audience="staff",
        staff=user,
        device_label=device_label[:100],
        refresh_hash=hashlib.sha256(refresh.encode()).hexdigest(),
        absolute_expires_at=timeutils.now() + timedelta(seconds=settings.STAFF_REFRESH_TTL),
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


def _tombstone_on_commit(sessions: Iterable[AuthSession]) -> None:
    values = {f"auth:sid:{session.pk}": f"revoked:{session.revoke_reason}" for session in sessions}
    if values:
        transaction.on_commit(lambda: cache.set_many(values, timeout=settings.STAFF_ACCESS_TTL + 60))


@transaction.atomic
def revoke_session(session: AuthSession, reason: str) -> None:
    stored = AuthSession.objects.select_for_update().get(pk=session.pk)
    if stored.revoked_at is None:
        stored.revoked_at = timeutils.now()
        stored.revoke_reason = reason
        stored.save(update_fields=["revoked_at", "revoke_reason"])
    session.revoked_at = stored.revoked_at
    session.revoke_reason = stored.revoke_reason
    _tombstone_on_commit([stored])


@transaction.atomic
def revoke_user_sessions(user: StaffUser, reason: str, *, except_sid: uuid.UUID | None = None) -> int:
    return len(_revoke_user_sessions(user, reason, except_sid=except_sid))


def _revoke_user_sessions(
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
    _tombstone_on_commit(sessions)
    return sessions


@transaction.atomic
def deactivate_staff(user: StaffUser, *, actor: StaffUser, request: Any = None) -> int:
    """Deactivate ``user`` and revoke every open session with reason ``deactivated``.

    Returns the number of revoked sessions; an already inactive user revokes nothing (0).
    """
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.is_active:
        user.is_active = False
        return 0
    stored.is_active = False
    stored.save(update_fields=["is_active"])
    user.is_active = False
    sessions = _revoke_user_sessions(stored, "deactivated")
    audit.record(
        action="auth.staff_deactivated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before={"is_active": True},
        after={"revoked_count": len(sessions)},
    )
    for session in sessions:
        audit.record(
            action="auth.session_revoked",
            entity_type="auth_session",
            entity_id=str(session.pk),
            actor=actor,
            request=request,
            after={"sid": str(session.pk), "reason": "deactivated"},
        )
    return len(sessions)


@transaction.atomic
def reactivate_staff(user: StaffUser, *, actor: StaffUser, request: Any = None) -> bool:
    """Reactivate ``user``. Returns False when the user was already active (no audit is written)."""
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if stored.is_active:
        user.is_active = True
        return False
    stored.is_active = True
    stored.save(update_fields=["is_active"])
    user.is_active = True
    audit.record(
        action="auth.staff_reactivated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before={"is_active": False},
        after={"is_active": True},
    )
    return True
