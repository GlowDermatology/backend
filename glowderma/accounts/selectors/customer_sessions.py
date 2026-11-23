"""Reads of customer sign-in sessions (B-W1: customer contract §2.2.10, admin §2.6)."""

from __future__ import annotations

from django.core.exceptions import ValidationError

from glowderma.accounts.models import (
    AuthSession,
)
from glowderma.core import timeutils


def customer_session_open(session_id: str) -> bool:
    """Whether the customer session ``session_id`` is open (not revoked, not past its absolute expiry): a
    loyalty QR token is honoured only while the session that issued it is (B-W1)."""
    try:
        return AuthSession.objects.filter(
            pk=session_id,
            audience="customer",
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        ).exists()
    except ValidationError:  # not a UUID
        return False


__all__ = [
    "customer_session_open",
]
