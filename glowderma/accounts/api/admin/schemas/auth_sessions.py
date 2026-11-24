"""OpenAPI declarations for staff sign-in sessions: password login (with the MFA challenge), refresh and
logout."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.schemas.common import ERRORS, LOGIN_RESULT
from glowderma.accounts.api.admin.serializers import (
    AccessSerializer,
    LoginSerializer,
    LogoutSerializer,
    RefreshSerializer,
)

LOGIN_POST = extend_schema(
    operation_id="admin_auth_login",
    description="Email + password sign-in. With the admin panel MFA on (feat/admin-ux) a correct "
    "password opens no session: the answer is `{mfa_required: true, challenge, challenge_expires_at}` "
    "and `auth/mfa/verify/` opens it.",
    auth=[],
    request=LoginSerializer,
    responses={200: LOGIN_RESULT, **ERRORS},
)

REFRESH_POST = extend_schema(
    operation_id="admin_auth_refresh",
    auth=[],
    request=RefreshSerializer,
    responses={200: AccessSerializer, **ERRORS},
)

LOGOUT_POST = extend_schema(
    operation_id="admin_auth_logout", auth=[], request=LogoutSerializer, responses={204: None, **ERRORS}
)


__all__ = [
    "LOGIN_POST",
    "LOGOUT_POST",
    "REFRESH_POST",
]
