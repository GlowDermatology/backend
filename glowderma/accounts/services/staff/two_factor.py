"""The admin panel's second factor in the staff services (feat/admin-ux, ``accounts.mfa``): completing a
sign-in with a TOTP or recovery code, and a manager's reset of another member's factor.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from glowderma.accounts import mfa, second_factor
from glowderma.accounts.errors import MfaChallengeInvalid
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.services.common import revocation
from glowderma.accounts.services.staff import auth_sessions, credentials, messages
from glowderma.core import alerts, audit, timeutils
from glowderma.core.api.exceptions import Conflict

logger = logging.getLogger("glowderma.accounts.services")


def complete_mfa_login(
    raw_challenge: object,
    *,
    code: str | None = None,
    recovery_code: str | None = None,
    device_label: str,
    request: Any = None,
) -> tuple[AuthSession, str]:
    """``auth/mfa/verify/``: the session of a sign-in that answered its challenge with a TOTP ``code`` or a
    ``recovery_code`` (``auth_method`` = the challenge's first step, ``mfa_verified_at`` stamped).

    400 ``mfa_challenge_invalid`` for an unknown, expired, used-up or exhausted challenge, or one whose
    account meanwhile changed its password, was deactivated or lost its panel MFA (the challenge is dropped);
    400 ``mfa_code_invalid`` for a wrong code (the challenge stays for the remaining attempts); 429 while the
    ``panel_otp`` lockout runs. The challenge is single use: claimed under the staff row lock right before the
    session opens. Audited ``auth.mfa_succeeded`` and ``auth.login_succeeded`` (``mfa``: the factor), plus
    ``auth.mfa_recovery_used`` (``remaining``) for a recovery code."""
    pending = mfa.open_challenge(raw_challenge)
    if pending is None:
        raise MfaChallengeInvalid
    user = StaffUser.objects.filter(pk=pending.staff_id).first()
    if user is None or not mfa.challenge_matches(pending, user):
        mfa.drop_challenge(pending)
        raise MfaChallengeInvalid
    # a running lockout answers 429 before the challenge's attempts are used
    mfa.check_attempt(user, request, reserve=False)
    if not mfa.count_attempt(pending):
        raise MfaChallengeInvalid
    used = mfa.use_factor(user, request, code=code, recovery_code=recovery_code, context="login")
    social = pending.method != AuthSession.AuthMethod.PASSWORD
    with transaction.atomic():
        locked = StaffUser.objects.select_for_update().get(pk=user.pk)
        # a social first step needs its identity still linked (an unlink since kills the challenge)
        unlinked = social and (
            pending.identity_id is None
            or not SocialIdentity.objects.filter(
                pk=pending.identity_id, staff=locked, provider=pending.method
            ).exists()
        )
        if unlinked:
            mfa.drop_challenge(pending)
            raise MfaChallengeInvalid
        if not mfa.challenge_matches(pending, locked) or not mfa.claim_challenge(pending):
            raise MfaChallengeInvalid
        session, refresh = auth_sessions.start_staff_session(
            locked,
            device_label=device_label,
            auth_method=pending.method,
            failure_code="social_account_not_allowed" if social else "invalid_credentials",
        )
        instant = timeutils.now()
        session.mfa_verified_at = instant
        session.save(update_fields=["mfa_verified_at"])
        if social and pending.identity_id is not None:
            SocialIdentity.objects.filter(pk=pending.identity_id, staff=locked).update(last_login_at=instant)
        audit.record(
            action="auth.mfa_succeeded",
            entity_type="staff",
            entity_id=str(locked.pk),
            actor=locked,
            request=request,
            after={"factor": used.factor, "method": pending.method},
        )
        audit.record(
            action="auth.login_succeeded",
            entity_type="staff",
            entity_id=str(locked.pk),
            actor=locked,
            request=request,
            after={"email": locked.email, "method": pending.method, "mfa": used.factor},
        )
        if used.recovery_codes_left is not None:
            audit.record(
                action="auth.mfa_recovery_used",
                entity_type="staff",
                entity_id=str(locked.pk),
                actor=locked,
                request=request,
                after={"remaining": used.recovery_codes_left},
            )
    return session, refresh


@transaction.atomic
def reset_staff_mfa(
    user: StaffUser,
    *,
    actor: StaffUser | None,
    request: Any = None,
    reason: str = "",
    refuse_self: bool = True,
    via: str = "api",
    protect_superuser: bool = True,
) -> dict[str, int]:
    """A lost phone: delete ``user``'s admin panel TOTP devices and recovery codes (console devices stay),
    revoke every open session (``admin_revoked``) and end a running ``panel_otp`` lockout once it commits. 409
    for the caller's own account (``refuse_self``; they disable it in Settings), for an account without panel
    MFA, and for a superuser unless the caller is one too (``protect_superuser``; review SEC-2 — ``manage.py
    resetstaffmfa`` passes ``False``). Audited ``auth.mfa_reset`` (``via``: api, cli) plus
    ``auth.session_revoked`` per session; alerted ``auth.mfa_reset`` (ids only), since a manager who also
    resets the password could sign in as the person."""
    if refuse_self and actor is not None and actor.pk == user.pk:
        raise Conflict(messages.MFA_SELF_RESET)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if protect_superuser and not credentials.superuser_may_be_taken_by(stored, actor):
        raise Conflict(messages.MFA_SUPERUSER_RESET)
    panel = second_factor.PANEL
    if not panel.totp_devices(stored).exists() and not panel.recovery_devices(stored).exists():
        raise Conflict(messages.MFA_NOT_ENABLED_FOR)
    removed = second_factor.reset_devices(stored, scope=second_factor.PANEL)
    sessions = auth_sessions.revoke_user_sessions_locked(stored, "admin_revoked")
    credentials.unlock_on_commit(stored.email, scopes=(mfa.GUARD_SCOPE,))
    details: dict[str, Any] = {**removed, "revoked_count": len(sessions), "via": via}
    audit.record(
        action="auth.mfa_reset",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        after=details,
        reason=reason,
    )
    revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
    logger.warning("auth.mfa_reset staff_id=%s via=%s", stored.pk, via)
    alerts.alert("auth.mfa_reset", {"staff_id": stored.pk, "via": via}, scope=str(stored.pk))
    return {**removed, "revoked_count": len(sessions)}


__all__ = ["complete_mfa_login", "reset_staff_mfa"]
