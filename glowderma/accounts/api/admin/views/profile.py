"""The signed-in staff member's own profile (``auth/me/``)."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services
from glowderma.accounts.api.admin.payloads import me_payload
from glowderma.accounts.api.admin.schemas.profile import ME_GET, ME_PATCH
from glowderma.accounts.api.admin.serializers import (
    MeUpdateSerializer,
)
from glowderma.accounts.api.base import WorkforceAPIView
from glowderma.accounts.models import AuthSession, StaffUser


class MeView(WorkforceAPIView):
    allow_password_change_pending = True

    @ME_GET
    def get(self, request: Request) -> Response:
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(me_payload(cast(StaffUser, request.user), session))

    @ME_PATCH
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
