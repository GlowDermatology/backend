"""Managing the staff directory (``staff/``, admin contract §2.16)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle

from glowderma.accounts import selectors, services
from glowderma.accounts.api.admin.payloads import (
    TEMPORARY_PASSWORD,
    activation_payload,
    open_password_link,
    staff_credentials,
    staff_member_payload,
)
from glowderma.accounts.api.admin.schemas.staff_members import (
    STAFF_CREATE,
    STAFF_DEACTIVATE,
    STAFF_LIST,
    STAFF_PARTIAL_UPDATE,
    STAFF_REACTIVATE,
    STAFF_RESET_MFA,
    STAFF_RESET_PASSWORD,
    STAFF_RETRIEVE,
    STAFF_SEND_PASSWORD_LINK,
)
from glowderma.accounts.api.admin.serializers import (
    StaffCreateSerializer,
    StaffListItemSerializer,
    StaffListQuerySerializer,
    StaffMemberSerializer,
    StaffResetMfaSerializer,
    StaffResetPasswordSerializer,
    StaffUpdateSerializer,
)
from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.accounts.api.throttles import (
    PasswordLinkThrottle,
)
from glowderma.accounts.models import StaffUser
from glowderma.accounts.permissions import IsManager
from glowderma.core import enums
from glowderma.core.idempotency import idempotent


class StaffViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    """``staff/`` (contract §2.16): the directory every staff member reads (the audit-log actor filter) and
    the staff accounts managers manage (``StaffUser.is_manager``, decision GST-1)."""

    serializer_class = StaffListItemSerializer
    queryset = StaffUser.objects.none()  # the schema's model; rows come from get_queryset
    page_size_options = (20, 50, 100)
    default_page_size = 50

    def get_permissions(self) -> Sequence[BasePermission]:
        if self.action == "list":
            return cast(Sequence[BasePermission], super().get_permissions())
        return [IsManager()]

    def get_throttles(self) -> list[BaseThrottle]:
        throttles = super().get_throttles()
        return [*throttles, PasswordLinkThrottle()] if self.action == "send_password_link" else throttles

    def _is_manager(self) -> bool:
        user = self.request.user
        return bool(getattr(user, "is_manager", False) or getattr(user, "is_superuser", False))

    def get_queryset(self) -> QuerySet[StaffUser]:
        query = StaffListQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        filters = cast(selectors.StaffFilters, dict(query.validated_data))
        return selectors.staff_list(filters, members=self._is_manager())

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "manager": self._is_manager()}

    def _target(self, pk: int) -> StaffUser:
        user = StaffUser.objects.filter(pk=pk).first()
        if user is None:
            raise NotFound
        return user

    @STAFF_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @STAFF_CREATE
    @idempotent(sealed=(TEMPORARY_PASSWORD,))
    def create(self, request: Request) -> Response:
        body = StaffCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        user, password = services.create_staff(
            email=data["email"],
            full_name=data["full_name"],
            is_manager=data["is_manager"],
            actor=cast(StaffUser, request.user),
            request=request,
            delivery=data["delivery"],
            language=data["language"],
        )
        return Response(staff_credentials(user, password, data["delivery"]), status=201)

    @STAFF_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        user = selectors.staff_member(int(pk))
        if user is None:
            raise NotFound
        return Response(StaffMemberSerializer(user).data)

    @STAFF_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        body = StaffUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.update_staff(
            user, dict(body.validated_data), actor=cast(StaffUser, request.user), request=request
        )
        return Response(staff_member_payload(user.pk))

    @STAFF_DEACTIVATE
    @idempotent()
    def deactivate(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        services.deactivate_staff(user, actor=cast(StaffUser, request.user), request=request)
        return Response(staff_member_payload(user.pk))

    @STAFF_REACTIVATE
    @idempotent(required=False)
    def reactivate(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        services.reactivate_staff(user, actor=cast(StaffUser, request.user), request=request)
        return Response(staff_member_payload(user.pk))

    @STAFF_RESET_PASSWORD
    @idempotent(sealed=(TEMPORARY_PASSWORD,))
    def reset_password(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        body = StaffResetPasswordSerializer(data=request.data if isinstance(request.data, dict) else {})
        body.is_valid(raise_exception=True)
        delivery = body.validated_data["delivery"]
        password, _ = services.reset_staff_password(
            user, actor=cast(StaffUser, request.user), request=request, delivery=delivery
        )
        link = (
            open_password_link(user) if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD else None
        )
        return Response(
            {
                "staff": staff_member_payload(user.pk),
                TEMPORARY_PASSWORD: password,
                "activation": activation_payload(link),
            }
        )

    @STAFF_SEND_PASSWORD_LINK
    @idempotent(required=False)
    def send_password_link(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        token = services.send_password_link(user, actor=cast(StaffUser, request.user), request=request)
        return Response({"staff": staff_member_payload(user.pk), "activation": activation_payload(token)})

    @STAFF_RESET_MFA
    @idempotent(required=False)
    def reset_mfa(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        body = StaffResetMfaSerializer(data=request.data if isinstance(request.data, dict) else {})
        body.is_valid(raise_exception=True)
        services.reset_staff_mfa(
            user, actor=cast(StaffUser, request.user), request=request, reason=body.validated_data["reason"]
        )
        return Response(staff_member_payload(user.pk))


__all__ = [
    "StaffViewSet",
]
