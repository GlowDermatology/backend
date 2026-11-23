"""Staff sign-out (admin contract §2.1 ``auth/logout/``): the current session, every other one, or all."""

from __future__ import annotations

from typing import Any, cast

from django.db import transaction

from glowderma.accounts.models import StaffUser
from glowderma.accounts.services.common import revocation
from glowderma.accounts.services.staff import auth_sessions
from glowderma.core import audit
from glowderma.core.api.exceptions import AuthFailed


def sign_out_staff(raw_refresh: str, scope: str, *, request: Any) -> int:
    """End the sessions ``scope`` names (``current``, ``others``, ``all``) of the account the refresh token
    belongs to; an unknown or ended token is 401 ``SESSION_EXPIRED``. The token is looked up first (that
    lookup commits its own ``last_used_at`` stamp), then the sessions end and ``auth.logout`` is audited in
    one transaction. Returns how many sessions ended."""
    try:
        session = auth_sessions.refresh_staff_session(raw_refresh)
    except AuthFailed as exc:
        raise AuthFailed("SESSION_EXPIRED") from exc
    user = cast(StaffUser, session.staff)
    with transaction.atomic():
        if scope == "current":
            revocation.revoke_session(session, "logout")
            count = 1
        else:
            count = auth_sessions.revoke_user_sessions(
                user, "logout", except_sid=session.pk if scope == "others" else None
            )
        audit.record(
            action="auth.logout",
            entity_type="staff",
            entity_id=str(user.pk),
            actor=user,
            request=request,
            after={"scope": scope, "revoked_count": count},
        )
    return count


__all__ = ["sign_out_staff"]
