"""The audit trail of the admin panel MFA: every event is a ``staff`` entry by the staff member themself."""

from __future__ import annotations

from typing import Any

from glowderma.accounts.models import StaffUser
from glowderma.core import audit


def record_mfa_event(action: str, user: StaffUser, request: Any, **after: Any) -> None:
    audit.record(
        action=action,
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after=after or None,
    )


__all__ = [
    "record_mfa_event",
]
