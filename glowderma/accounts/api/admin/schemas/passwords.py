"""OpenAPI declarations for staff passwords: changing one's own, and the e-mailed set-password links and their
tokens."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.schemas.common import ERRORS
from glowderma.accounts.api.admin.serializers import (
    ChangePasswordSerializer,
    OwnPasswordLinkSerializer,
    PasswordTokenInfoSerializer,
    PasswordTokenSerializer,
    PasswordTokenSetSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)

CHANGE_PASSWORD_POST = extend_schema(
    operation_id="admin_auth_change_password",
    request=ChangePasswordSerializer,
    responses={204: None, **ERRORS},
)

PASSWORD_TOKEN_CHECK_POST = extend_schema(
    operation_id="admin_auth_password_token_check",
    description="Public (no bearer): whose password link this is, for the set-password page. An unknown, "
    "used, revoked or expired link, or one of an inactive account → one uniform 400 `token_invalid`.",
    auth=[],
    request=PasswordTokenSerializer,
    responses={200: PasswordTokenInfoSerializer, **ERRORS},
)

PASSWORD_TOKEN_SET_POST = extend_schema(
    operation_id="admin_auth_password_token_set",
    description="Public (no bearer): set the password with a link (single use). The password is "
    "validated for the link's account (the change-password codes, `password_unchanged` for the current "
    "one); every session of the account is revoked and no new one is opened — the person signs in next. "
    "A bad link → 400 `token_invalid`.",
    auth=[],
    request=PasswordTokenSetSerializer,
    responses={204: None, **ERRORS},
)

OWN_PASSWORD_LINK_POST = extend_schema(
    operation_id="admin_auth_password_link",
    description="E-mail the caller a single-use link to set their password, for an account that must "
    "change it but cannot type the current one (signed in with a social identity, an e-mail-link "
    "account). 409 `conflict` when no change is pending or e-mail links are not available "
    "(`capabilities.staff_email_invite`); 5/hour.",
    request=None,
    responses={200: OwnPasswordLinkSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CHANGE_PASSWORD_POST",
    "OWN_PASSWORD_LINK_POST",
    "PASSWORD_TOKEN_CHECK_POST",
    "PASSWORD_TOKEN_SET_POST",
]
