"""Audit entries of the console sign-in (``AuditEntry.action`` ``console.*``, entity ``staff``).

``console.login_failed`` / ``console.login_locked`` (password step), ``console.password_verified``,
``console.otp_failed`` / ``console.otp_locked`` (code, recovery and enrolment steps),
``console.2fa_enrolled``, ``console.recovery_code_used``, ``console.login_succeeded`` (``method``: totp,
recovery, enrolment, or password when OPS_REQUIRE_2FA is off), ``console.logout`` and
``console.session_expired`` (``reason``: idle, absolute, unstamped). Never a password, a code or a secret:
the e-mail typed on a failed login is kept as typed (the admin API's ``auth.login_failed`` does the same).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from glowderma.accounts.models import StaffUser
from glowderma.core import audit


def record(
    action: str,
    request: HttpRequest | None,
    user: StaffUser | None = None,
    *,
    email: str = "",
    reason: str = "",
    **after: Any,
) -> None:
    audit.record(
        action=action,
        entity_type="staff",
        entity_id=str(user.pk) if user is not None else "",
        actor=user,
        request=request,
        after={"email": user.email if user is not None else email, "channel": "ops_console", **after},
        reason=reason,
    )
