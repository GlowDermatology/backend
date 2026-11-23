"""Using one factor of the admin panel MFA: a TOTP code or a recovery code, under the lockout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glowderma.accounts import second_factor
from glowderma.accounts.mfa import lockout, policy
from glowderma.accounts.models import StaffUser


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
            device = second_factor.verify_totp(user, totp, scope=policy.SCOPE)
            return FactorUse(policy.FACTOR_TOTP, None) if device is not None else None

        return lockout.guarded(user, request, factor=policy.FACTOR_TOTP, context=context, attempt=verify_code)
    recovery = recovery_code or ""

    def verify_recovery() -> FactorUse | None:
        used = second_factor.use_recovery_code(user, recovery, scope=policy.SCOPE)
        return FactorUse(policy.FACTOR_RECOVERY, used.remaining) if used is not None else None

    return lockout.guarded(
        user, request, factor=policy.FACTOR_RECOVERY, context=context, attempt=verify_recovery
    )


__all__ = [
    "FactorUse",
    "use_factor",
]
