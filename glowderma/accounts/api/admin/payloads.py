"""The response bodies of the staff admin API, built once for every view that answers them."""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound

from glowderma.accounts import mfa, selectors, services, tokens
from glowderma.accounts.api.admin.serializers import (
    StaffMemberSerializer,
)
from glowderma.accounts.models import AuthSession, StaffPasswordToken, StaffUser
from glowderma.core import enums, timeutils

TEMPORARY_PASSWORD = "temporary_password"  # noqa: S105 - the response key, not a password


def me_payload(user: StaffUser, session: AuthSession) -> dict[str, Any]:
    return {
        "id": user.pk,
        "email": user.email,
        "display_name": user.full_name or user.email,
        "role": user.role,
        "is_superuser": user.is_superuser,
        "is_manager": user.is_manager or user.is_superuser,
        "linked_providers": list(
            user.social_identities.order_by("provider").values_list("provider", flat=True)
        ),
        "session_expires_at": timeutils.isoformat(session.absolute_expires_at),
        # how the current session's sign-in was proven (contract §2.1); null for a session from before 0007
        "auth_method": session.auth_method or None,
        # feat/admin-ux: the forced change gate, the panel language, the TOTP state and what the server offers
        "must_change_password": user.must_change_password,
        "language": user.language,
        "mfa_enabled": mfa.enabled(user),
        "capabilities": {
            "staff_email_invite": services.staff_email_invite_available(),
        },
    }


def activation_payload(token: StaffPasswordToken | None) -> dict[str, Any] | None:
    if token is None:
        return None
    return {"sent": True, "expires_at": timeutils.isoformat(token.expires_at)}


def open_password_link(user: StaffUser) -> StaffPasswordToken | None:
    """The link a create or reset just issued (the newest open one), for the answer's ``activation``."""
    return (
        StaffPasswordToken.objects.filter(staff=user, used_at__isnull=True, revoked_at__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )


def mfa_challenge_payload(challenge: mfa.Challenge) -> dict[str, Any]:
    return {
        "mfa_required": True,
        "challenge": challenge.raw,
        "challenge_expires_at": timeutils.isoformat(challenge.expires_at),
    }


def access_payload(session: AuthSession) -> dict[str, Any]:
    access, expires = tokens.issue_access(session)
    return {
        "access": access,
        "access_expires_at": timeutils.isoformat(expires),
        "refresh_expires_at": timeutils.isoformat(session.absolute_expires_at),
    }


def mfa_status_payload(user: StaffUser) -> dict[str, Any]:
    current = mfa.status(user)
    return {"enabled": current.enabled, "recovery_codes_left": current.recovery_codes_left}


def staff_member_payload(staff_id: int) -> dict[str, Any]:
    """The ``StaffMember`` of a fresh read (every write answers with it)."""
    user = selectors.staff_member(staff_id)
    if user is None:  # pragma: no cover - the write just locked the row
        raise NotFound
    return dict(StaffMemberSerializer(user).data)


def staff_credentials(user: StaffUser, password: str | None, delivery: str) -> dict[str, Any]:
    link = open_password_link(user) if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD else None
    return {
        "staff": staff_member_payload(user.pk),
        TEMPORARY_PASSWORD: password,
        "activation": activation_payload(link),
    }


__all__ = [
    "TEMPORARY_PASSWORD",
    "access_payload",
    "activation_payload",
    "me_payload",
    "mfa_challenge_payload",
    "mfa_status_payload",
    "open_password_link",
    "staff_credentials",
    "staff_member_payload",
]
