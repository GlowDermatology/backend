"""OpenAPI declarations for staff social sign-in and linking (``auth/oauth/{provider}/``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.schemas.common import LOGIN_RESULT, PROVIDER_PARAM
from glowderma.accounts.api.admin.serializers import (
    MeSerializer,
    OAuthCallbackSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    PUBLIC_ERRORS,
    ErrorEnvelopeSerializer,
)

O_AUTH_PROVIDER_POST = extend_schema(
    operation_id="admin_auth_oauth_login",
    description=(
        "Staff social sign-in, allow-list only: Django exchanges the authorization code with the "
        "provider and signs in the staff member whose identity is already linked. Same response as "
        "auth/login/ (with the admin panel MFA on: the challenge for auth/mfa/verify/)."
    ),
    auth=[],
    parameters=[PROVIDER_PARAM],
    request=OAuthCallbackSerializer,
    responses={
        200: LOGIN_RESULT,
        **PUBLIC_ERRORS,
        404: ErrorEnvelopeSerializer,
        503: ErrorEnvelopeSerializer,
    },
)

O_AUTH_PROVIDER_DELETE = extend_schema(
    operation_id="admin_auth_oauth_unlink",
    description="Unlink the caller's identity for the provider.",
    parameters=[PROVIDER_PARAM],
    responses={204: None, **ADMIN_ERRORS},
)

O_AUTH_LINK_POST = extend_schema(
    operation_id="admin_auth_oauth_link",
    description=(
        "Link the provider identity to the signed-in staff member (same body as the sign-in); returns Me "
        "with the updated linked_providers."
    ),
    parameters=[PROVIDER_PARAM],
    request=OAuthCallbackSerializer,
    responses={200: MeSerializer, **ADMIN_ERRORS, 503: ErrorEnvelopeSerializer},
)


__all__ = [
    "O_AUTH_LINK_POST",
    "O_AUTH_PROVIDER_DELETE",
    "O_AUTH_PROVIDER_POST",
]
