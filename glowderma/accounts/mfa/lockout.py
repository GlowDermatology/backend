"""The lockouts of the admin panel MFA: the per-account ladder over the codes (with its long-window streak
bound), the guarded code check, and the current-password re-authentication of the MFA settings."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from rest_framework.exceptions import ErrorDetail, Throttled, ValidationError

from glowderma.accounts.errors import MfaCodeInvalid
from glowderma.accounts.mfa import audit_trail, policy
from glowderma.accounts.models import StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import alerts

logger = logging.getLogger("glowderma.accounts.mfa")


def _locked(user: StaffUser, request: Any, wait: int, **extra: int) -> None:
    """Audit ``auth.mfa_locked``, log and alert. The password (or the social sign-in) was right but the codes
    were not: a stolen password, or a lost phone. Ids and counts only."""
    audit_trail.record_mfa_event("auth.mfa_locked", user, request, wait=wait, **extra)
    details = "".join(f" {name}={value}" for name, value in extra.items())
    logger.warning("auth.mfa_locked staff_id=%s wait=%s%s", user.pk, wait, details)
    alerts.alert("auth.mfa_locked", {"staff_id": user.pk, "wait": wait, **extra}, scope=str(user.pk))


def check_attempt(user: StaffUser, request: Any, *, reserve: bool = True) -> None:
    """Reserve one code attempt for ``user`` (``LoginGuard`` :data:`GUARD_SCOPE`; ``reserve=False`` only
    checks for a running lockout); during a lockout audit ``auth.mfa_locked``, log, alert and re-raise the
    429."""
    try:
        LoginGuard().check(policy.GUARD_SCOPE, email_hash(user.email), reserve=reserve)
    except Throttled as exc:
        _locked(user, request, int(math.ceil(float(getattr(exc, "wait", None) or 0))))
        raise


def _failed(user: StaffUser, request: Any, *, factor: str, context: str) -> MfaCodeInvalid:
    lockout = LoginGuard().record_failure(
        policy.GUARD_SCOPE, email_hash(user.email), streak_limit=policy.STREAK_LIMIT
    )
    audit_trail.record_mfa_event("auth.mfa_failed", user, request, factor=factor, context=context)
    if lockout is not None and lockout.streak and lockout.streak % policy.STREAK_LIMIT == 0:
        # the long-window bound tripped: alert now, whether or not the guesser comes back during the lock
        _locked(user, request, lockout.wait, failures=lockout.streak)
    return MfaCodeInvalid()


def _succeeded(user: StaffUser) -> None:
    LoginGuard().reset(policy.GUARD_SCOPE, email_hash(user.email))


def guarded[T](
    user: StaffUser, request: Any, *, factor: str, context: str, attempt: Callable[[], T | None]
) -> T:
    """Run one code check under the lockout: ``attempt`` returns ``None`` for a wrong code (400
    ``mfa_code_invalid``, a failure counted and audited)."""
    check_attempt(user, request)
    result = attempt()
    if result is None:
        raise _failed(user, request, factor=factor, context=context)
    _succeeded(user)
    return result


def check_current_password(user: StaffUser, password: str) -> None:
    """The re-authentication of the MFA settings (``auth/change-password/``'s pattern and lockout): a wrong
    password → 400 ``password_incorrect`` on ``current_password`` and one failure of the ``change_password``
    ladder (429 while it is locked)."""
    guard = LoginGuard()
    hashed = email_hash(user.email)
    guard.check(policy.PASSWORD_GUARD_SCOPE, hashed)
    stored = StaffUser.objects.only("password").get(pk=user.pk)
    if not stored.check_password(password):
        guard.record_failure(policy.PASSWORD_GUARD_SCOPE, hashed)
        raise ValidationError(
            {"current_password": [ErrorDetail(policy.PASSWORD_INCORRECT, code="password_incorrect")]}
        )
    guard.reset(policy.PASSWORD_GUARD_SCOPE, hashed)


def check_password_lockout(user: StaffUser) -> None:
    """The running ``change_password`` lockout only (no attempt reserved): before the body is validated."""
    LoginGuard().check(policy.PASSWORD_GUARD_SCOPE, email_hash(user.email), reserve=False)


__all__ = [
    "check_attempt",
    "check_current_password",
    "check_password_lockout",
    "guarded",
]
