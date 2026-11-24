"""A specialist's staff account: the invite and the role change."""

from __future__ import annotations

from typing import cast

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import selectors as account_selectors
from glowderma.accounts.api.admin.payloads import TEMPORARY_PASSWORD, staff_credentials
from glowderma.accounts.api.admin.serializers import (
    StaffMemberSerializer,
    WorkforceInviteSerializer,
)
from glowderma.accounts.api.base import AdminAPIView
from glowderma.accounts.models import StaffUser
from glowderma.accounts.permissions import IsManager
from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.admin.schemas.workforce import (
    SPECIALIST_ACCOUNT_ROLE_POST,
    SPECIALIST_INVITE_POST,
)
from glowderma.professionals.api.admin.serializers.workforce import (
    SpecialistRoleChangeSerializer,
)


class SpecialistInviteView(AdminAPIView):
    permission_classes = [IsManager]

    @SPECIALIST_INVITE_POST
    @idempotent(sealed=(TEMPORARY_PASSWORD,))
    def post(self, request: Request) -> Response:
        body = WorkforceInviteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user, password, professional = services.invite_specialist(
            dict(body.validated_data), actor=cast(StaffUser, request.user), request=request
        )
        credentials = staff_credentials(user, password, body.validated_data["delivery"])
        return Response({**credentials, "specialist_id": professional.pk}, status=201)


class SpecialistAccountRoleView(AdminAPIView):
    permission_classes = [IsManager]

    @SPECIALIST_ACCOUNT_ROLE_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        body = SpecialistRoleChangeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        _, profile, upcoming = services.set_specialist_account_role(
            pk, body.validated_data["role"], actor=cast(StaffUser, request.user), request=request
        )
        member = account_selectors.staff_member(pk)
        if member is None:  # pragma: no cover - the service just locked the row
            raise NotFound
        return Response(
            {
                "account": StaffMemberSerializer(member).data,
                "specialist_id": None if profile is None else profile.pk,
                "upcoming_appointments": upcoming,
            }
        )


__all__ = [
    "SpecialistAccountRoleView",
    "SpecialistInviteView",
]
