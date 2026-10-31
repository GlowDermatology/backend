"""Who may use the ops console: ``StaffUser.is_staff`` (separate from the Next.js admin panel, which never
reads it) plus the second factor. Every change is audited (``ops_access.*``, entity ``staff``); the actor is
the staff member who did it, or ``system`` for the ``manage.py ops_access`` command without ``--actor``.

* ``grant`` — ``is_staff`` on; the user enrols a TOTP device at the first console login.
* ``revoke`` — ``is_staff`` off, every TOTP device and recovery code deleted, every console session ended.
* ``reset_two_factor`` — devices and recovery codes deleted (sessions stop being verified at once): the next
  login enrols again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.http import HttpRequest
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.console import state, twofactor
from glowderma.accounts.models import StaffUser
from glowderma.core import audit


def _record(
    action: str,
    user: StaffUser,
    *,
    actor: StaffUser | None,
    request: HttpRequest | None,
    reason: str,
    **after: Any,
) -> None:
    audit.record(
        action=action,
        entity_type="staff",
        entity_id=str(user.pk),
        actor=actor,
        request=request,
        after={"email": user.email, **after},
        reason=reason,
    )


@transaction.atomic
def grant(
    user: StaffUser, *, actor: StaffUser | None, reason: str, request: HttpRequest | None = None
) -> bool:
    """Give ``user`` console access; ``False`` when it already had it (nothing written)."""
    if user.is_staff:
        return False
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    _record("ops_access.granted", user, actor=actor, request=request, reason=reason, is_staff=True)
    return True


@transaction.atomic
def revoke(
    user: StaffUser, *, actor: StaffUser | None, reason: str, request: HttpRequest | None = None
) -> dict[str, int]:
    removed = twofactor.reset_devices(user)
    sessions = state.end_console_sessions(user)
    if user.is_staff:
        user.is_staff = False
        user.save(update_fields=["is_staff"])
    result = {**removed, "sessions": sessions}
    _record("ops_access.revoked", user, actor=actor, request=request, reason=reason, is_staff=False, **result)
    return result


@transaction.atomic
def reset_two_factor(
    user: StaffUser, *, actor: StaffUser | None, reason: str, request: HttpRequest | None = None
) -> dict[str, int]:
    removed = twofactor.reset_devices(user)
    _record("ops_access.2fa_reset", user, actor=actor, request=request, reason=reason, **removed)
    return removed


@dataclass(frozen=True)
class AccessStatus:
    email: str
    is_active: bool
    is_staff: bool
    is_manager: bool
    totp_confirmed: bool
    totp_pending: bool
    recovery_codes_left: int
    last_login: str


def status(user: StaffUser) -> AccessStatus:
    return AccessStatus(
        email=user.email,
        is_active=user.is_active,
        is_staff=user.is_staff,
        is_manager=user.is_manager,
        totp_confirmed=TOTPDevice.objects.filter(user=user, confirmed=True).exists(),
        totp_pending=TOTPDevice.objects.filter(user=user, confirmed=False).exists(),
        recovery_codes_left=twofactor.recovery_codes_left(user),
        last_login=user.last_login.isoformat() if user.last_login else "-",
    )
