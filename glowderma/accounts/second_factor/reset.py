"""Removing a staff member's second factor in one scope (a manager's reset)."""

from __future__ import annotations

from django.db import transaction

from glowderma.accounts.models import StaffUser
from glowderma.accounts.second_factor.scopes import DeviceScope


def reset_devices(user: StaffUser, *, scope: DeviceScope) -> dict[str, int]:
    """Delete every TOTP device and recovery code of ``user`` in ``scope``: the next login enrols again, and
    a console session verified by a deleted device stops being verified at once (``OTPMiddleware`` finds no
    device). The other scope's devices stay."""
    with transaction.atomic():
        totp, _ = scope.totp_devices(user).delete()
        codes = scope.recovery_tokens(user).count()
        scope.recovery_devices(user).delete()
    return {"totp_devices": totp, "recovery_codes": codes}


__all__ = [
    "reset_devices",
]
