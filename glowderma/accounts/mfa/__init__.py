"""The admin panel's optional TOTP second factor (feat/admin-ux, admin contract §2.1 "MFA").

Each staff member's own choice, off by default (nobody can mandate it). The devices live in the
:data:`~glowderma.accounts.second_factor.PANEL` scope of ``accounts.second_factor`` — separate from the ops
console's: enabling, using or disabling the panel factor never touches a console device, and the console never
accepts a panel code, so the console's enrolment window and four-eyes rule stay what they are.

* **Enable** (``auth/mfa/setup/`` → ``confirm/``): the current password, then a new unconfirmed device (it
  replaces an earlier unconfirmed one) shown as an ``otpauth://`` URI (issuer
  :data:`second_factor.PANEL_ISSUER`) and a manual key; the first valid code confirms it, removes the scope's
  other TOTP devices and issues ten recovery codes (shown once, stored as keyed hashes, single use). Audited
  ``auth.mfa_enabled``.
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

The package is a facade over ``policy`` (scopes, limits, texts), ``audit_trail``, ``state``, ``lockout``,
``factors``, ``enrolment`` and ``challenges``.
"""

from glowderma.accounts.mfa.challenges import (
    Challenge,
    MfaRequired,
    PendingSignIn,
    challenge_matches,
    claim_challenge,
    count_attempt,
    drop_challenge,
    issue_challenge,
    open_challenge,
)
from glowderma.accounts.mfa.enrolment import (
    Setup,
    begin_setup,
    confirm_setup,
    disable,
    regenerate_recovery_codes,
)
from glowderma.accounts.mfa.factors import (
    FactorUse,
    use_factor,
)
from glowderma.accounts.mfa.lockout import (
    check_attempt,
    check_current_password,
    check_password_lockout,
    guarded,
)
from glowderma.accounts.mfa.policy import (
    CHALLENGE_MAX_ATTEMPTS,
    CHALLENGE_MAX_LENGTH,
    CHALLENGE_TTL_SECONDS,
    FACTOR_RECOVERY,
    FACTOR_TOTP,
    GUARD_SCOPE,
    MFA_ALREADY_ENABLED,
    MFA_NOT_ENABLED,
    PASSWORD_GUARD_SCOPE,
    PASSWORD_INCORRECT,
    SCOPE,
    STREAK_LIMIT,
)
from glowderma.accounts.mfa.state import (
    Status,
    enabled,
    status,
)

__all__ = [
    "CHALLENGE_MAX_ATTEMPTS",
    "CHALLENGE_MAX_LENGTH",
    "CHALLENGE_TTL_SECONDS",
    "FACTOR_RECOVERY",
    "FACTOR_TOTP",
    "GUARD_SCOPE",
    "MFA_ALREADY_ENABLED",
    "MFA_NOT_ENABLED",
    "PASSWORD_GUARD_SCOPE",
    "PASSWORD_INCORRECT",
    "SCOPE",
    "STREAK_LIMIT",
    "Challenge",
    "FactorUse",
    "MfaRequired",
    "PendingSignIn",
    "Setup",
    "Status",
    "begin_setup",
    "challenge_matches",
    "check_attempt",
    "check_current_password",
    "check_password_lockout",
    "claim_challenge",
    "confirm_setup",
    "count_attempt",
    "disable",
    "drop_challenge",
    "enabled",
    "guarded",
    "issue_challenge",
    "open_challenge",
    "regenerate_recovery_codes",
    "status",
    "use_factor",
]
