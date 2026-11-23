"""A staff member changing their own password (admin contract §2.1 ``auth/change-password/``), under the
``change_password`` lockout: the current password re-authenticates, every other session ends."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from rest_framework.exceptions import ErrorDetail, ValidationError

from glowderma.accounts.models import StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.accounts.services.staff import auth_sessions, messages, password_links
from glowderma.core import audit

CHANGE_PASSWORD_SCOPE = "change_password"  # noqa: S105 - a lockout scope name, shared with the panel MFA


def check_password_change_lockout(user: StaffUser) -> None:
    """The running ``change_password`` lockout only (no attempt reserved): before the body is validated."""
    LoginGuard().check(CHANGE_PASSWORD_SCOPE, email_hash(user.email), reserve=False)


def change_own_password(
    user: StaffUser, *, current_password: str, new_password: str, keep_session_id: uuid.UUID, request: Any
) -> None:
    """Set ``new_password`` after ``current_password`` re-authenticates (one attempt of the lockout; a wrong
    one is 400 ``password_incorrect`` and a counted failure). The same password again is 400
    ``password_unchanged`` (the current one was right: not a failed guess). Every session but
    ``keep_session_id`` ends; audited ``auth.password_changed`` (``forced``: a pending forced change)."""
    guard = LoginGuard()
    hashed = email_hash(user.email)
    guard.check(CHANGE_PASSWORD_SCOPE, hashed)
    with transaction.atomic():
        stored = StaffUser.objects.select_for_update().get(pk=user.pk)
        if not stored.check_password(current_password):
            guard.record_failure(CHANGE_PASSWORD_SCOPE, hashed)
            raise ValidationError(
                {"current_password": [ErrorDetail(messages.PASSWORD_INCORRECT, code="password_incorrect")]}
            )
        if stored.check_password(new_password):
            guard.reset(CHANGE_PASSWORD_SCOPE, hashed)
            raise ValidationError(
                {"new_password": [ErrorDetail(messages.PASSWORD_UNCHANGED, code="password_unchanged")]}
            )
        stored.set_password(new_password)
        forced = password_links.record_own_password_change(stored)
        auth_sessions.revoke_user_sessions(stored, "password_changed", except_sid=keep_session_id)
        audit.record(
            action="auth.password_changed",
            entity_type="staff",
            entity_id=str(stored.pk),
            actor=stored,
            request=request,
            after={"forced": forced},
        )
    guard.reset(CHANGE_PASSWORD_SCOPE, hashed)


__all__ = ["CHANGE_PASSWORD_SCOPE", "change_own_password", "check_password_change_lockout"]
