"""The admin panel's optional TOTP second factor (feat/admin-ux, admin contract §2.1 "MFA").

Each staff member's own choice, off by default (nobody can mandate it). The devices live in the
:data:`~glowderma.accounts.console.twofactor.PANEL` scope of ``console.twofactor`` — separate from the ops
console's: enabling, using or disabling the panel factor never touches a console device, and the console never
accepts a panel code, so the console's enrolment window and four-eyes rule stay what they are.

* **Enable** (``auth/mfa/setup/`` → ``confirm/``): the current password, then a new unconfirmed device (it
  replaces an earlier unconfirmed one) shown as an ``otpauth://`` URI (issuer :data:`twofactor.PANEL_ISSUER`)
  and a manual key; the first valid code confirms it, removes the scope's other TOTP devices and issues ten
  recovery codes (shown once, stored as keyed hashes, single use). Audited ``auth.mfa_enabled``.
* **Disable** / **new recovery codes**: the current password plus a code (disable also takes a recovery
  code). Audited ``auth.mfa_disabled`` / ``auth.mfa_recovery_codes_regenerated``.
* **Sign-in**: a correct password (``auth/login/``) or a social sign-in (``auth/oauth/{provider}/``) of an
  account with a confirmed panel device opens no session: it answers a challenge
  (:func:`issue_challenge`, audited ``auth.mfa_challenged``). The raw challenge (32 random bytes, base64url)
  is returned once; the cache holds only its SHA-256 for :data:`CHALLENGE_TTL_SECONDS` with the account, the
  sign-in method and the account's session auth hash (an HMAC of the password hash), so the challenge dies
  when the password changes. ``auth/mfa/verify/`` (``services.complete_mfa_login``) takes a code or a
  recovery code: at most :data:`CHALLENGE_MAX_ATTEMPTS` attempts per challenge, single use (claimed with an
  atomic ``cache.add``), and it dies with a deactivation or an MFA reset too.
* **Lockout**: every code check (sign-in, confirm, disable, new codes) goes through ``LoginGuard`` scope
  :data:`GUARD_SCOPE` (five failures a minute lock the account for 1, 2, 4, 8, then 15 minutes) with a
  long-window bound: :data:`STREAK_LIMIT` wrong codes in a row (no correct code between, none more than
  ``login_lockout.STREAK_TTL_SECONDS`` apart) lock it for 15 minutes, then 1, 4, 16 and 24 hours at each
  further
  :data:`STREAK_LIMIT` — so a guesser who keeps under five a minute (the challenge's own cap is renewed by
  signing in again) is stopped as well; that lock is audited ``auth.mfa_locked`` (``wait``, ``failures``) and
  alerted when it starts. A locked attempt is audited ``auth.mfa_locked``, logged and alerted
  (``auth.mfa_locked``, one alert per account per ``ALERT_DEDUPE_MINUTES``). A wrong code is audited
  ``auth.mfa_failed`` (``context``: login, setup, disable, recovery_codes). Password re-checks share the
  ``change_password`` lockout of ``auth/change-password/``.
* **Replay**: a TOTP code verifies under the device row lock and moves django-otp's ``last_t`` forward, so the
  same code (or an older one) is refused afterwards.
* **Lost phone**: a recovery code, else a manager's ``POST staff/{id}/reset-mfa/`` or ``manage.py
  resetstaffmfa`` (``services.reset_staff_mfa``).

Never logged or audited: a password, a code, a recovery code, the TOTP secret or the raw challenge.
"""

from __future__ import annotations

import hashlib
import logging
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.core.cache import cache
from django.db import transaction
from rest_framework.exceptions import ErrorDetail, Throttled, ValidationError

from glowderma.accounts.console import twofactor
from glowderma.accounts.errors import MfaCodeInvalid
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import alerts, audit, timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.uzsource import N_

logger = logging.getLogger(__name__)

SCOPE = twofactor.PANEL
GUARD_SCOPE = "panel_otp"
PASSWORD_GUARD_SCOPE = "change_password"  # noqa: S105 - a lockout scope name, shared with change-password
CHALLENGE_TTL_SECONDS = 300
CHALLENGE_MAX_ATTEMPTS = 5
CHALLENGE_MAX_LENGTH = 128
# Wrong codes in a row that lock the account whatever their pace (``LoginGuard`` ``streak_limit``; NIST SP
# 800-63B §5.2.2 allows at most 100).
STREAK_LIMIT = 10
FACTOR_TOTP = "totp"
FACTOR_RECOVERY = "recovery"

MFA_ALREADY_ENABLED = N_("Ikki bosqichli himoya allaqachon yoqilgan.")
MFA_NOT_ENABLED = N_("Ikki bosqichli himoya yoqilmagan.")
PASSWORD_INCORRECT = N_("Joriy parol noto‘g‘ri.")


def enabled(user: StaffUser) -> bool:
    """Whether ``user`` has a confirmed admin panel TOTP device (``me.mfa_enabled``)."""
    return twofactor.has_second_factor(user, scope=SCOPE)


@dataclass(frozen=True)
class Status:
    enabled: bool
    recovery_codes_left: int


def status(user: StaffUser) -> Status:
    on = enabled(user)
    return Status(on, twofactor.recovery_codes_left(user, scope=SCOPE) if on else 0)


# --- guards ------------------------------------------------------------------------------------------------


def _record(action: str, user: StaffUser, request: Any, **after: Any) -> None:
    audit.record(
        action=action,
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after=after or None,
    )


def _locked(user: StaffUser, request: Any, wait: int, **extra: int) -> None:
    """Audit ``auth.mfa_locked``, log and alert. The password (or the social sign-in) was right but the codes
    were not: a stolen password, or a lost phone. Ids and counts only."""
    _record("auth.mfa_locked", user, request, wait=wait, **extra)
    details = "".join(f" {name}={value}" for name, value in extra.items())
    logger.warning("auth.mfa_locked staff_id=%s wait=%s%s", user.pk, wait, details)
    alerts.alert("auth.mfa_locked", {"staff_id": user.pk, "wait": wait, **extra}, scope=str(user.pk))


def check_attempt(user: StaffUser, request: Any, *, reserve: bool = True) -> None:
    """Reserve one code attempt for ``user`` (``LoginGuard`` :data:`GUARD_SCOPE`; ``reserve=False`` only
    checks for a running lockout); during a lockout audit ``auth.mfa_locked``, log, alert and re-raise the
    429."""
    try:
        LoginGuard().check(GUARD_SCOPE, email_hash(user.email), reserve=reserve)
    except Throttled as exc:
        _locked(user, request, int(math.ceil(float(getattr(exc, "wait", None) or 0))))
        raise


def _failed(user: StaffUser, request: Any, *, factor: str, context: str) -> MfaCodeInvalid:
    lockout = LoginGuard().record_failure(GUARD_SCOPE, email_hash(user.email), streak_limit=STREAK_LIMIT)
    _record("auth.mfa_failed", user, request, factor=factor, context=context)
    if lockout is not None and lockout.streak and lockout.streak % STREAK_LIMIT == 0:
        # the long-window bound tripped: alert now, whether or not the guesser comes back during the lock
        _locked(user, request, lockout.wait, failures=lockout.streak)
    return MfaCodeInvalid()


def _succeeded(user: StaffUser) -> None:
    LoginGuard().reset(GUARD_SCOPE, email_hash(user.email))


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


@dataclass(frozen=True)
class FactorUse:
    factor: str
    recovery_codes_left: int | None  # after a recovery code; ``None`` for a TOTP code


def use_factor(
    user: StaffUser,
    request: Any,
    *,
    code: str | None = None,
    recovery_code: str | None = None,
    context: str,
) -> FactorUse:
    """Check a TOTP ``code`` or consume a ``recovery_code`` of the panel scope (one of them), under the
    lockout."""
    if code:
        totp = code

        def verify_code() -> FactorUse | None:
            device = twofactor.verify_totp(user, totp, scope=SCOPE)
            return FactorUse(FACTOR_TOTP, None) if device is not None else None

        return guarded(user, request, factor=FACTOR_TOTP, context=context, attempt=verify_code)
    recovery = recovery_code or ""

    def verify_recovery() -> FactorUse | None:
        used = twofactor.use_recovery_code(user, recovery, scope=SCOPE)
        return FactorUse(FACTOR_RECOVERY, used.remaining) if used is not None else None

    return guarded(user, request, factor=FACTOR_RECOVERY, context=context, attempt=verify_recovery)


def check_current_password(user: StaffUser, password: str) -> None:
    """The re-authentication of the MFA settings (``auth/change-password/``'s pattern and lockout): a wrong
    password → 400 ``password_incorrect`` on ``current_password`` and one failure of the ``change_password``
    ladder (429 while it is locked)."""
    guard = LoginGuard()
    hashed = email_hash(user.email)
    guard.check(PASSWORD_GUARD_SCOPE, hashed)
    stored = StaffUser.objects.only("password").get(pk=user.pk)
    if not stored.check_password(password):
        guard.record_failure(PASSWORD_GUARD_SCOPE, hashed)
        raise ValidationError(
            {"current_password": [ErrorDetail(PASSWORD_INCORRECT, code="password_incorrect")]}
        )
    guard.reset(PASSWORD_GUARD_SCOPE, hashed)


def check_password_lockout(user: StaffUser) -> None:
    """The running ``change_password`` lockout only (no attempt reserved): before the body is validated."""
    LoginGuard().check(PASSWORD_GUARD_SCOPE, email_hash(user.email), reserve=False)


# --- enable, disable, new recovery codes -------------------------------------------------------------------


@dataclass(frozen=True)
class Setup:
    device_id: int
    otpauth_uri: str
    manual_key: str


def begin_setup(user: StaffUser) -> Setup:
    """A new unconfirmed panel device (replacing an earlier unconfirmed one); 409 when MFA is already on.
    The caller checked the current password."""
    if enabled(user):
        raise Conflict(MFA_ALREADY_ENABLED)
    device = twofactor.enrolment_device(user, None, scope=SCOPE)
    device.user = user
    return Setup(
        device_id=int(device.pk),
        otpauth_uri=twofactor.otpauth_uri(device, scope=SCOPE),
        manual_key=twofactor.manual_key(device),
    )


def confirm_setup(
    user: StaffUser, device_id: int, code: str, *, session_id: Any = None, request: Any = None
) -> list[str]:
    """Confirm the pending device with its first code: the ten recovery codes (returned once). The current
    session counts as verified from now on (``mfa_verified_at``). 409 when MFA is already on; a wrong code or
    a device that is not the caller's pending panel device → 400 ``mfa_code_invalid``."""
    if enabled(user):
        raise Conflict(MFA_ALREADY_ENABLED)

    def confirm() -> twofactor.Enrolment | None:
        return twofactor.confirm_enrolment(user, device_id, code, scope=SCOPE)

    enrolment = guarded(user, request, factor=FACTOR_TOTP, context="setup", attempt=confirm)
    with transaction.atomic():
        if session_id is not None:
            AuthSession.objects.filter(pk=session_id, staff=user).update(mfa_verified_at=timeutils.now())
        _record("auth.mfa_enabled", user, request, recovery_codes=len(enrolment.recovery_codes))
    return enrolment.recovery_codes


def disable(user: StaffUser, *, code_or_recovery: str, request: Any = None) -> dict[str, int]:
    """Remove the panel devices and recovery codes after a TOTP code or a recovery code (the caller checked
    the current password); 409 when MFA is off."""
    if not enabled(user):
        raise Conflict(MFA_NOT_ENABLED)
    value = code_or_recovery.strip()
    if twofactor.TOTP_CODE.match(value):
        used = use_factor(user, request, code=value, context="disable")
    else:
        used = use_factor(user, request, recovery_code=value, context="disable")
    with transaction.atomic():
        removed = twofactor.reset_devices(user, scope=SCOPE)
        _record("auth.mfa_disabled", user, request, factor=used.factor, **removed)
    return removed


def regenerate_recovery_codes(user: StaffUser, *, code: str, request: Any = None) -> list[str]:
    """Ten new recovery codes (the old ones stop working) after a TOTP code; 409 when MFA is off."""
    if not enabled(user):
        raise Conflict(MFA_NOT_ENABLED)
    use_factor(user, request, code=code.strip(), context="recovery_codes")
    with transaction.atomic():
        codes = twofactor.issue_recovery_codes(user, scope=SCOPE)
        _record("auth.mfa_recovery_codes_regenerated", user, request, recovery_codes=len(codes))
    return codes


# --- the sign-in challenge ---------------------------------------------------------------------------------


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
    expires_at = timeutils.now() + timedelta(seconds=CHALLENGE_TTL_SECONDS)
    cache.set(
        _key(_digest(raw)),
        {
            "uid": user.pk,
            "method": method,
            "identity": identity_id,
            "marker": user.get_session_auth_hash(),
            "exp": expires_at.timestamp(),
        },
        timeout=CHALLENGE_TTL_SECONDS,
    )
    _record("auth.mfa_challenged", user, request, email=user.email, method=method)
    return Challenge(raw, expires_at)


def open_challenge(raw: object) -> PendingSignIn | None:
    """The pending sign-in of ``raw``, or ``None`` (malformed, unknown, expired)."""
    if not isinstance(raw, str) or not raw or len(raw) > CHALLENGE_MAX_LENGTH:
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
    cache.add(key, 0, timeout=CHALLENGE_TTL_SECONDS)
    try:
        count = int(cache.incr(key))
    except ValueError:  # expired between the two calls: the challenge is gone with it
        count = CHALLENGE_MAX_ATTEMPTS + 1
    if count > CHALLENGE_MAX_ATTEMPTS:
        drop_challenge(pending)
        return False
    return True


def claim_challenge(pending: PendingSignIn) -> bool:
    """Use the challenge up (once, across workers); ``False`` when another request already did."""
    claimed = bool(cache.add(_used_key(pending.digest), 1, timeout=CHALLENGE_TTL_SECONDS))
    drop_challenge(pending)
    return claimed


def challenge_matches(pending: PendingSignIn, user: StaffUser | None) -> bool:
    """The challenge still belongs to an active account whose password and panel MFA are unchanged."""
    return (
        user is not None
        and user.pk == pending.staff_id
        and user.is_active
        and secrets.compare_digest(user.get_session_auth_hash(), pending.marker)
        and enabled(user)
    )
