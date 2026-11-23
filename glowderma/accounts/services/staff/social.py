"""Staff social sign-in and identity links (admin contract §2.1): the Google auto-link policy, sign-in with a
linked identity, and linking / unlinking from a password session.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound, PermissionDenied

from glowderma.accounts import mfa
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.services.staff import auth_sessions, messages
from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import AuthFailed, Conflict
from glowderma.integrations.oauth.ports import SocialClaims


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

    An account with the admin panel MFA on (feat/admin-ux) gets no session here: an automatic link is kept
    and :class:`~glowderma.accounts.mfa.MfaRequired` is raised with the sign-in challenge
    (``auth/mfa/verify/`` opens the session).
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
        if user is not None and identity is not None and mfa.enabled(user):
            challenge = mfa.issue_challenge(user, provider, identity_id=identity.pk, request=request)
        elif user is not None and identity is not None:
            session, refresh = auth_sessions.start_staff_session(
                user,
                device_label=device_label,
                auth_method=provider,
                failure_code="social_account_not_allowed",
            )
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
        else:
            challenge = None
    if challenge is not None:
        raise mfa.MfaRequired(challenge)  # after the commit: an automatic link stays
    # Outside the transaction so the audit row survives the 401 (the view's exception handler rolls back).
    audit.record(
        action="auth.login_failed",
        entity_type="staff",
        entity_id="",
        request=request,
        after={"email": claims.email, "method": provider},
    )
    raise AuthFailed("social_account_not_allowed")


def require_password_session(session_id: uuid.UUID | str) -> AuthSession:
    """The staff session ``session_id`` when its sign-in was an email + password one (admin contract §2.1:
    identities are linked only by a staff member signed in with the password), else 403
    ``permission_denied``. A session from before ``accounts.0007`` (no recorded method) is refused too."""
    session = AuthSession.objects.filter(pk=session_id, audience="staff").first()
    if session is None or session.auth_method != AuthSession.AuthMethod.PASSWORD:
        raise PermissionDenied
    return session


@transaction.atomic
def link_identity(
    user: StaffUser,
    provider: str,
    claims: SocialClaims,
    *,
    session_id: uuid.UUID | str | None = None,
    request: Any = None,
) -> SocialIdentity:
    """Link ``(provider, subject)`` to ``user`` (contract 2.1 ``POST auth/oauth/{provider}/link/``).

    Only from a password session (``session_id``; 403 ``permission_denied`` for a social one, so a social
    sign-in cannot attach a second identity). One identity per ``(staff, provider)`` and per ``(provider,
    subject)``; either duplicate is a 409.
    """
    if session_id is None:
        raise PermissionDenied
    require_password_session(session_id)
    if SocialIdentity.objects.filter(staff=user, provider=provider).exists():
        raise Conflict(messages.SOCIAL_PROVIDER_LINKED)
    if SocialIdentity.objects.filter(provider=provider, subject=claims.subject).exists():
        raise Conflict(messages.SOCIAL_SUBJECT_LINKED)
    try:
        with transaction.atomic():
            identity = SocialIdentity.objects.create(
                staff=user, provider=provider, subject=claims.subject, email_at_link=claims.email
            )
    except IntegrityError as exc:  # lost a race with a concurrent link
        raise Conflict(messages.SOCIAL_SUBJECT_LINKED) from exc
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


__all__ = ["social_login", "require_password_session", "link_identity", "unlink_identity"]
