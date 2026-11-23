"""Turning the admin panel MFA on (setup, then confirm with the first code), off, and replacing its recovery
codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from glowderma.accounts import second_factor
from glowderma.accounts.mfa import audit_trail, factors, lockout, policy, state
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core import timeutils
from glowderma.core.api.exceptions import Conflict


@dataclass(frozen=True)
class Setup:
    device_id: int
    otpauth_uri: str
    manual_key: str


def begin_setup(user: StaffUser) -> Setup:
    """A new unconfirmed panel device (replacing an earlier unconfirmed one); 409 when MFA is already on.
    The caller checked the current password."""
    if state.enabled(user):
        raise Conflict(policy.MFA_ALREADY_ENABLED)
    device = second_factor.enrolment_device(user, None, scope=policy.SCOPE)
    device.user = user
    return Setup(
        device_id=int(device.pk),
        otpauth_uri=second_factor.otpauth_uri(device, scope=policy.SCOPE),
        manual_key=second_factor.manual_key(device),
    )


def confirm_setup(
    user: StaffUser, device_id: int, code: str, *, session_id: Any = None, request: Any = None
) -> list[str]:
    """Confirm the pending device with its first code: the ten recovery codes (returned once). The current
    session counts as verified from now on (``mfa_verified_at``). 409 when MFA is already on; a wrong code or
    a device that is not the caller's pending panel device → 400 ``mfa_code_invalid``."""
    if state.enabled(user):
        raise Conflict(policy.MFA_ALREADY_ENABLED)

    def confirm() -> second_factor.Enrolment | None:
        return second_factor.confirm_enrolment(user, device_id, code, scope=policy.SCOPE)

    enrolment = lockout.guarded(user, request, factor=policy.FACTOR_TOTP, context="setup", attempt=confirm)
    with transaction.atomic():
        if session_id is not None:
            AuthSession.objects.filter(pk=session_id, staff=user).update(mfa_verified_at=timeutils.now())
        audit_trail.record_mfa_event(
            "auth.mfa_enabled", user, request, recovery_codes=len(enrolment.recovery_codes)
        )
    return enrolment.recovery_codes


def disable(user: StaffUser, *, code_or_recovery: str, request: Any = None) -> dict[str, int]:
    """Remove the panel devices and recovery codes after a TOTP code or a recovery code (the caller checked
    the current password); 409 when MFA is off."""
    if not state.enabled(user):
        raise Conflict(policy.MFA_NOT_ENABLED)
    value = code_or_recovery.strip()
    if second_factor.TOTP_CODE.match(value):
        used = factors.use_factor(user, request, code=value, context="disable")
    else:
        used = factors.use_factor(user, request, recovery_code=value, context="disable")
    with transaction.atomic():
        removed = second_factor.reset_devices(user, scope=policy.SCOPE)
        audit_trail.record_mfa_event("auth.mfa_disabled", user, request, factor=used.factor, **removed)
    return removed


def regenerate_recovery_codes(user: StaffUser, *, code: str, request: Any = None) -> list[str]:
    """Ten new recovery codes (the old ones stop working) after a TOTP code; 409 when MFA is off."""
    if not state.enabled(user):
        raise Conflict(policy.MFA_NOT_ENABLED)
    factors.use_factor(user, request, code=code.strip(), context="recovery_codes")
    with transaction.atomic():
        codes = second_factor.issue_recovery_codes(user, scope=policy.SCOPE)
        audit_trail.record_mfa_event(
            "auth.mfa_recovery_codes_regenerated", user, request, recovery_codes=len(codes)
        )
    return codes


__all__ = [
    "Setup",
    "begin_setup",
    "confirm_setup",
    "disable",
    "regenerate_recovery_codes",
]
