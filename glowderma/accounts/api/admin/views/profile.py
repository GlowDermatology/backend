"""The signed-in staff member's own profile (``auth/me/``)."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services
from glowderma.accounts.api.admin.payloads import me_payload
from glowderma.accounts.api.admin.serializers import (
    MeSerializer,
    MeUpdateSerializer,
)
from glowderma.accounts.api.admin.views.schema import ERRORS
from glowderma.accounts.api.base import WorkforceAPIView
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)


class MeView(WorkforceAPIView):
    allow_password_change_pending = True

    @extend_schema(operation_id="admin_auth_me", responses={200: MeSerializer, **ERRORS})
    def get(self, request: Request) -> Response:
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(me_payload(cast(StaffUser, request.user), session))

    @extend_schema(
        operation_id="admin_auth_me_update",
        description="Change the caller's admin panel language (`uz`, `ru`, `en`); returns Me. Allowed "
        "while a password change is pending.",
        request=MeUpdateSerializer,
        responses={200: MeSerializer, **ADMIN_ERRORS},
    )
    def patch(self, request: Request) -> Response:
        body = MeUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = cast(StaffUser, request.user)
        services.set_staff_language(user, body.validated_data["language"])
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(me_payload(user, session))


__all__ = [
    "MeView",
]
