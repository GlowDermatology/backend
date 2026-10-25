import hashlib
import secrets
import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound

from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import AuthFailed, Conflict
from glowderma.integrations.oauth.ports import SocialClaims

SOCIAL_SUBJECT_LINKED = "Bu ijtimoiy hisob allaqachon bog‘langan."
SOCIAL_PROVIDER_LINKED = "Bu provayder allaqachon bog‘langan."


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


def _google_is_authoritative(claims: SocialClaims) -> bool:
    """Google vouches for the address: verified, and either a consumer @gmail.com account or a Workspace
    account whose ``hd`` is the email's own domain (and the configured ``GOOGLE_ALLOWED_HD`` when set)."""
    if not claims.email_verified or "@" not in claims.email:
        return False
    domain = claims.email.rsplit("@", 1)[1].lower()
    if domain == "gmail.com":
        return True
    if not claims.hd or claims.hd.lower() != domain:
        return False
    allowed = str(settings.GOOGLE_ALLOWED_HD).lower()
    return not allowed or claims.hd.lower() == allowed


def _autolink_candidate(provider: str, claims: SocialClaims) -> StaffUser | None:
    """The active staff user a first Google sign-in may be linked to (``STAFF_GOOGLE_AUTOLINK``), else None.

    One query: the user must have the (case-insensitive) email and no identity for the provider yet.
    """
    if provider != "google" or not settings.STAFF_GOOGLE_AUTOLINK or not _google_is_authoritative(claims):
        return None
    return (
        StaffUser.objects.filter(email__iexact=claims.email, is_active=True)
        .exclude(social_identities__provider=provider)
        .first()
    )


def _record_link(user: StaffUser, provider: str, claims: SocialClaims, *, auto: bool, request: Any) -> None:
    audit.record(
        action="auth.social_linked",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after={"provider": provider, "subject": claims.subject, "email": claims.email, "auto": auto},
    )


def social_login(
    provider: str, claims: SocialClaims, *, device_label: str, request: Any = None
) -> tuple[AuthSession, str]:
    """Sign a staff member in with verified provider claims (contract 2.1 ``POST auth/oauth/{provider}/``).

    Allow-list only: the identity ``(provider, subject)`` must already be linked to an active staff user,
    or (Google only, ``STAFF_GOOGLE_AUTOLINK``) an active staff user must carry the verified email so the
    identity can be linked on first use. Anything else is one uniform ``social_account_not_allowed``,
    whether or not the email belongs to anyone.
    """
    identity = (
        SocialIdentity.objects.select_related("staff")
        .filter(provider=provider, subject=claims.subject)
        .first()
    )
    user: StaffUser | None = None
    if identity is not None:
        user = identity.staff if identity.staff.is_active else None
    with transaction.atomic():
        if identity is None:
            user = _autolink_candidate(provider, claims)
            if user is not None:
                identity = SocialIdentity.objects.create(
                    staff=user, provider=provider, subject=claims.subject, email_at_link=claims.email
                )
                _record_link(user, provider, claims, auto=True, request=request)
        if user is not None and identity is not None:
            session, refresh = start_staff_session(user, device_label=device_label)
            identity.last_login_at = timeutils.now()
            identity.save(update_fields=["last_login_at"])
            audit.record(
                action="auth.login_succeeded",
                entity_type="staff",
                entity_id=str(user.pk),
                actor=user,
                request=request,
                after={"email": user.email, "method": provider},
            )
            return session, refresh
    # Outside the transaction so the audit row survives the 401 (the view's exception handler rolls back).
    audit.record(
        action="auth.login_failed",
        entity_type="staff",
        entity_id="",
        request=request,
        after={"email": claims.email, "method": provider},
    )
    raise AuthFailed("social_account_not_allowed")


@transaction.atomic
def link_identity(
    user: StaffUser, provider: str, claims: SocialClaims, *, request: Any = None
) -> SocialIdentity:
    """Link ``(provider, subject)`` to ``user`` (contract 2.1 ``POST auth/oauth/{provider}/link/``).

    One identity per ``(staff, provider)`` and per ``(provider, subject)``; either duplicate is a 409.
    """
    if SocialIdentity.objects.filter(staff=user, provider=provider).exists():
        raise Conflict(SOCIAL_PROVIDER_LINKED)
    if SocialIdentity.objects.filter(provider=provider, subject=claims.subject).exists():
        raise Conflict(SOCIAL_SUBJECT_LINKED)
    try:
        with transaction.atomic():
            identity = SocialIdentity.objects.create(
                staff=user, provider=provider, subject=claims.subject, email_at_link=claims.email
            )
    except IntegrityError as exc:  # lost a race with a concurrent link
        raise Conflict(SOCIAL_SUBJECT_LINKED) from exc
    _record_link(user, provider, claims, auto=False, request=request)
    return identity


@transaction.atomic
def unlink_identity(user: StaffUser, provider: str, *, request: Any = None) -> None:
    """Remove the caller's identity for ``provider`` (contract 2.1 ``DELETE auth/oauth/{provider}/``)."""
    identity = SocialIdentity.objects.filter(staff=user, provider=provider).first()
    if identity is None:
        raise NotFound
    subject = identity.subject
    identity.delete()
    audit.record(
        action="auth.social_unlinked",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after={"provider": provider, "subject": subject},
    )
