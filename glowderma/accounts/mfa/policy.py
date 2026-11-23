"""The admin panel MFA's fixed values: its second-factor scope, lockout scopes, challenge limits and the texts
of its refusals."""

from __future__ import annotations

from glowderma.accounts import second_factor
from glowderma.core.uzsource import N_

SCOPE = second_factor.PANEL
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


__all__ = [
    "SCOPE",
    "GUARD_SCOPE",
    "PASSWORD_GUARD_SCOPE",
    "CHALLENGE_TTL_SECONDS",
    "CHALLENGE_MAX_ATTEMPTS",
    "CHALLENGE_MAX_LENGTH",
    "STREAK_LIMIT",
    "FACTOR_TOTP",
    "FACTOR_RECOVERY",
    "MFA_ALREADY_ENABLED",
    "MFA_NOT_ENABLED",
    "PASSWORD_INCORRECT",
]
