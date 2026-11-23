"""Staff password sign-in (admin contract §2.1 ``auth/login/``): the per-account lockout, the credentials, and
either a new session or, with the admin panel MFA on, the sign-in challenge its second step answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.contrib.auth import authenticate
from django.db import transaction

from glowderma.accounts import mfa
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.accounts.services.staff import auth_sessions
from glowderma.core import audit
from glowderma.core.api.exceptions import AuthFailed

LOGIN_SCOPE = "login"


@dataclass(frozen=True)
class PasswordSignIn:
    """A correct password: a ``session`` (with its ``refresh`` token), or the MFA ``challenge`` (no
    session)."""

    user: StaffUser
    session: AuthSession | None = None
    refresh: str | None = None
    challenge: mfa.Challenge | None = None


def password_sign_in(email: str, password: str, *, request: Any) -> PasswordSignIn:
    """Check ``email`` and ``password`` under the ``login`` lockout (an attempt is reserved before the
    password is checked, so concurrent guesses share the limit). A wrong password or an inactive account is
    counted, audited ``auth.login_failed`` and refused 401 ``invalid_credentials`` (committed even though the
    request fails); a right one resets the lockout and opens a session, audited ``auth.login_succeeded``, in
    one transaction."""
    hashed = email_hash(email)
    guard = LoginGuard()
    guard.check(LOGIN_SCOPE, hashed)
    user = authenticate(request=request, email=email, password=password)
    if not isinstance(user, StaffUser) or not user.is_active:
        guard.record_failure(LOGIN_SCOPE, hashed)
        audit.record(
            action="auth.login_failed",
            entity_type="staff",
            entity_id="",
            request=request,
            after={"email": email, "method": "password"},
        )
        raise AuthFailed("invalid_credentials")
    guard.reset(LOGIN_SCOPE, hashed)
    if mfa.enabled(user):
        challenge = mfa.issue_challenge(user, AuthSession.AuthMethod.PASSWORD, request=request)
        return PasswordSignIn(user, challenge=challenge)
    with transaction.atomic():
        session, refresh = auth_sessions.start_staff_session(
            user, device_label=request.META.get("HTTP_USER_AGENT", "")[:100]
        )
        audit.record(
            action="auth.login_succeeded",
            entity_type="staff",
            entity_id=str(user.pk),
            actor=user,
            request=request,
            after={"email": email, "method": "password"},
        )
    return PasswordSignIn(user, session=session, refresh=refresh)


__all__ = ["LOGIN_SCOPE", "PasswordSignIn", "password_sign_in"]
