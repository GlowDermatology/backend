"""Whether a staff member has the admin panel MFA on, and how many recovery codes are left."""

from __future__ import annotations

from dataclasses import dataclass

from glowderma.accounts import second_factor
from glowderma.accounts.mfa import policy
from glowderma.accounts.models import StaffUser


def enabled(user: StaffUser) -> bool:
    """Whether ``user`` has a confirmed admin panel TOTP device (``me.mfa_enabled``)."""
    return second_factor.has_second_factor(user, scope=policy.SCOPE)


@dataclass(frozen=True)
class Status:
    enabled: bool
    recovery_codes_left: int


def status(user: StaffUser) -> Status:
    on = enabled(user)
    return Status(on, second_factor.recovery_codes_left(user, scope=policy.SCOPE) if on else 0)


__all__ = [
    "Status",
    "enabled",
    "status",
]
