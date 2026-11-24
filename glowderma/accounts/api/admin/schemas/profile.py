"""OpenAPI declarations for the signed-in staff member's own profile (``auth/me/``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.schemas.common import ERRORS
from glowderma.accounts.api.admin.serializers import (
    MeSerializer,
    MeUpdateSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)

ME_GET = extend_schema(operation_id="admin_auth_me", responses={200: MeSerializer, **ERRORS})

ME_PATCH = extend_schema(
    operation_id="admin_auth_me_update",
    description="Change the caller's admin panel language (`uz`, `ru`, `en`); returns Me. Allowed "
    "while a password change is pending.",
    request=MeUpdateSerializer,
    responses={200: MeSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "ME_GET",
    "ME_PATCH",
]
