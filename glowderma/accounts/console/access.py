"""Who may use the ops console: ``StaffUser.is_staff`` (separate from the Next.js admin panel, which never
reads it) plus the second factor. Every change is audited (``ops_access.*``, entity ``staff``); the actor is
the staff member who did it, or ``system`` for the ``manage.py ops_access`` command without ``--actor``.

* ``grant`` — ``is_staff`` on; the user enrols a TOTP device at the first console login (inside the
  enrolment window it opens).
* ``revoke`` — ``is_staff`` off, every TOTP device and recovery code deleted, every console session ended.
* ``reset_two_factor`` — devices and recovery codes deleted and every console session of the user ended
  (logged in or with a sign-in pending): the next login enrols again, inside the window the reset opens.
* ``open_enrolment`` — a new window for a staff member who has console access but no device (``ops_access
  grant`` on such an account).

**Enrolment window.** Enrolment is trust-on-first-use with the password alone, so it is allowed only for
``OPS_ENROL_WINDOW_SECONDS`` after a grant, a 2FA reset or ``open_enrolment``; the first successful enrolment
(``console.2fa_enrolled``) or a revoke closes it. The window is read from the audit log itself — the latest
of these entries for the user (:data:`WINDOW_OPENERS`, :data:`WINDOW_CLOSERS`) — so it needs no schema, names
who opened it, and fails closed (no entry, a purged entry or an expired one: no enrolment). Staff who already
have a device are not affected.

**Four eyes.** A manager who resets a staff member's password in the Next admin panel
(``staff.password_reset``) and also opens that person's enrolment window from the console could sign in as
them with the temporary password and enrol their own phone. Enrolment is therefore refused when the window
was opened from the console by the same manager who reset the person's password within the window's length
before it (or any time after it opened): another manager, or the operator's ``manage.py ops_access
reset-2fa``, must open it. Both steps are audited either way, and ``console.2fa_enrolled`` records who opened
the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.console import state, twofactor
from glowderma.accounts.models import StaffUser
from glowderma.core import audit, timeutils
from glowderma.core.models import AuditEntry

# ``via`` of a window opener: the console's manager action, or the operator's command on the server.
VIA_CONSOLE = "console"
VIA_CLI = "cli"
WINDOW_OPENERS = ("ops_access.granted", "ops_access.2fa_reset", "ops_access.enrolment_opened")
WINDOW_CLOSERS = ("console.2fa_enrolled", "ops_access.revoked")
PASSWORD_RESET_ACTION = "staff.password_reset"  # noqa: S105 - an audit action name


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
    user: StaffUser,
    *,
    actor: StaffUser | None,
    reason: str,
    request: HttpRequest | None = None,
    via: str = VIA_CLI,
) -> bool:
    """Give ``user`` console access and open the enrolment window; ``False`` when it already had access
    (nothing written)."""
    if user.is_staff:
        return False
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    _record(
        "ops_access.granted",
        user,
        actor=actor,
        request=request,
        reason=reason,
        is_staff=True,
        via=via,
        enrol_until=_window_end().isoformat(),
    )
    return True


def _window_end(start: datetime | None = None) -> datetime:
    return (start or timeutils.now()) + timedelta(seconds=settings.OPS_ENROL_WINDOW_SECONDS)


@transaction.atomic
def open_enrolment(
    user: StaffUser,
    *,
    actor: StaffUser | None,
    reason: str,
    request: HttpRequest | None = None,
    via: str = VIA_CLI,
) -> datetime | None:
    """Open a new enrolment window for a staff member with console access and no confirmed device; its end,
    or ``None`` when nothing was written (no access, a device already, or a window still open)."""
    if not user.is_staff or twofactor.has_second_factor(user) or enrolment_window(user) is not None:
        return None
    until = _window_end()
    _record(
        "ops_access.enrolment_opened",
        user,
        actor=actor,
        request=request,
        reason=reason,
        via=via,
        enrol_until=until.isoformat(),
    )
    return until


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
    user: StaffUser,
    *,
    actor: StaffUser | None,
    reason: str,
    request: HttpRequest | None = None,
    via: str = VIA_CONSOLE,
) -> dict[str, int]:
    """Delete the devices and recovery codes, end every console session of ``user`` (a sign-in pending with
    the old factor included) and open the enrolment window."""
    removed = twofactor.reset_devices(user)
    result = {**removed, "sessions": state.end_console_sessions(user)}
    _record(
        "ops_access.2fa_reset",
        user,
        actor=actor,
        request=request,
        reason=reason,
        via=via,
        enrol_until=_window_end().isoformat(),
        **result,
    )
    return result


@dataclass(frozen=True)
class EnrolmentWindow:
    """An open enrolment window: the audit entry that opened it and until when it lasts."""

    audit_id: int
    action: str
    opened_at: datetime
    until: datetime
    opened_by_id: int | None
    opened_by: str
    via: str


def enrolment_window(user: StaffUser) -> EnrolmentWindow | None:
    """The user's open enrolment window, or ``None`` (never opened, closed by an enrolment or a revoke, or
    expired)."""
    entry = (
        AuditEntry.objects.filter(
            entity_type="staff", entity_id=str(user.pk), action__in=(*WINDOW_OPENERS, *WINDOW_CLOSERS)
        )
        .order_by("-id")
        .first()
    )
    if entry is None or entry.action not in WINDOW_OPENERS:
        return None
    until = _window_end(entry.created_at)
    if timeutils.now() >= until:
        return None
    details = entry.after if isinstance(entry.after, dict) else {}
    return EnrolmentWindow(
        audit_id=entry.pk,
        action=entry.action,
        opened_at=entry.created_at,
        until=until,
        opened_by_id=entry.actor_id if entry.actor_type == "staff" else None,
        opened_by=entry.actor_label,
        via=str(details.get("via") or VIA_CLI),
    )


# Why an enrolment is refused (``console.enrol_refused`` ``reason``).
NO_WINDOW = "no_window"
SAME_MANAGER = "same_manager"


def enrolment_refusal(user: StaffUser, window: EnrolmentWindow | None) -> str:
    """``""`` when ``user`` may enrol now, else :data:`NO_WINDOW` or :data:`SAME_MANAGER` (module doc)."""
    if window is None:
        return NO_WINDOW
    if window.via == VIA_CONSOLE and window.opened_by_id is not None:
        since = window.opened_at - timedelta(seconds=settings.OPS_ENROL_WINDOW_SECONDS)
        if AuditEntry.objects.filter(
            action=PASSWORD_RESET_ACTION,
            entity_type="staff",
            entity_id=str(user.pk),
            actor_type="staff",
            actor_id=window.opened_by_id,
            created_at__gte=since,
        ).exists():
            return SAME_MANAGER
    return ""


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
