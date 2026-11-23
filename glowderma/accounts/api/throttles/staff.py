"""Throttles of the staff API (admin contract §1.9): the public sign-in routes per client IP, the signed-in
workforce per staff member, and the password links per sender and target."""

from __future__ import annotations

import hashlib
from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles.base import ClientIPThrottle


class LoginRateThrottle(ClientIPThrottle):
    scope = "login"


class AuthClientIPThrottle(ClientIPThrottle):
    scope = "auth_client_ip"


class RefreshRateThrottle(ClientIPThrottle):
    scope = "refresh"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        data = request.data
        refresh = str(data.get("refresh", "")) if isinstance(data, dict) else ""
        return hashlib.sha256(refresh.encode()).hexdigest()


class StaffRateThrottle(ClientIPThrottle):
    scope = "staff"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        return str(request.user.pk)


class OAuthIPThrottle(ClientIPThrottle):
    """Contract 1.9: ``auth/oauth/{provider}/`` per client IP, 30/min by default."""

    scope = "oauth"


class PasswordTokenIPThrottle(ClientIPThrottle):
    """``auth/password-token/check/`` and ``set/`` (public) per client IP, 30/min by default."""

    scope = "staff_password_token"


class PasswordLinkThrottle(ClientIPThrottle):
    """E-mail password links per (sender, target): ``staff/{id}/send-password-link/`` keys on the manager
    and the target id, the self ``auth/password-link/`` on the caller twice; 5/hour by default, so nobody
    floods a mailbox through the relay."""

    scope = "staff_password_link"

    def cache_ident(self, request: Request, view: Any) -> str | None:
        target = str(getattr(view, "kwargs", {}).get("pk") or request.user.pk)
        return f"{request.user.pk}:{target}"


class MfaVerifyIPThrottle(ClientIPThrottle):
    """``auth/mfa/verify/`` (public, the sign-in's second step) per client IP, 30/min by default; the
    account's ``panel_otp`` lockout and the challenge's attempt cap bound the guessing per account."""

    scope = "mfa_verify"


__all__ = [
    "AuthClientIPThrottle",
    "LoginRateThrottle",
    "MfaVerifyIPThrottle",
    "OAuthIPThrottle",
    "PasswordLinkThrottle",
    "PasswordTokenIPThrottle",
    "RefreshRateThrottle",
    "StaffRateThrottle",
]
