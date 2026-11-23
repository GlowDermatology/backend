"""Managing the staff directory (``staff/``, admin contract §2.16)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
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
from glowderma.accounts.api.admin.serializers import (
    StaffCreateSerializer,
    StaffCredentialsSerializer,
    StaffLinkSentSerializer,
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
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.core.idempotency import idempotent

STAFF_ID_DESCRIPTION = "Manager only (403 `permission_denied` otherwise)."


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

    @extend_schema(
        operation_id="admin_staff_list",
        description=(
            "Every staff member: the directory behind the audit-log actor filter. A manager's rows also "
            "carry `full_name`, `is_manager`, `last_login_at`, `linked_providers`, `created_at` and "
            "(feat/admin-ux) `must_change_password`, `mfa_enabled` and `invite`; for anyone else these keys "
            "are `null`."
        ),
        parameters=[StaffListQuerySerializer],
        responses={200: StaffListItemSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_staff_create",
        description=(
            f"{STAFF_ID_DESCRIPTION} Creates an active staff account. `delivery`: `temporary_password` "
            "(generated, shown once in the answer), `email_link` (an invite link e-mailed to the account; no "
            "usable password until it is used) or `both`; either way the person must set their own password "
            "first (`must_change_password`). `email` already used by any account → `unique`; an e-mail "
            "delivery while `capabilities.staff_email_invite` is false → `delivery` `invalid_choice`."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=StaffCreateSerializer,
        responses={201: StaffCredentialsSerializer, **ADMIN_ERRORS},
    )
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

    @extend_schema(
        operation_id="admin_staff_retrieve",
        description=STAFF_ID_DESCRIPTION,
        responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        user = selectors.staff_member(int(pk))
        if user is None:
            raise NotFound
        return Response(StaffMemberSerializer(user).data)

    @extend_schema(
        operation_id="admin_staff_partial_update",
        description=(
            f"{STAFF_ID_DESCRIPTION} Changes `full_name` and/or `is_manager` (`email`, `is_active`, "
            "`password` → `read_only`). Removing the manager role of the caller or of the last active "
            "manager → 409 `conflict`."
        ),
        parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=StaffUpdateSerializer,
        responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        body = StaffUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.update_staff(
            user, dict(body.validated_data), actor=cast(StaffUser, request.user), request=request
        )
        return Response(staff_member_payload(user.pk))

    @extend_schema(
        operation_id="admin_staff_deactivate",
        description=(
            f"{STAFF_ID_DESCRIPTION} The account can no longer sign in: every open session is revoked "
            "(`deactivated`), linked identities stay. The caller's own account or the last active manager → "
            "409 `conflict`; an inactive account → 200 unchanged."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def deactivate(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        services.deactivate_staff(user, actor=cast(StaffUser, request.user), request=request)
        return Response(staff_member_payload(user.pk))

    @extend_schema(
        operation_id="admin_staff_reactivate",
        description=(
            f"{STAFF_ID_DESCRIPTION} The account may sign in again (password, identities and manager role "
            "kept); an active account → 200 unchanged."
        ),
        parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def reactivate(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        services.reactivate_staff(user, actor=cast(StaffUser, request.user), request=request)
        return Response(staff_member_payload(user.pk))

    @extend_schema(
        operation_id="admin_staff_reset_password",
        description=(
            f"{STAFF_ID_DESCRIPTION} Replaces the password and revokes every open session "
            "(`password_changed`) and every earlier link; linked identities stay. `delivery` (optional body, "
            "default `temporary_password`): a generated temporary password (shown once), `email_link` (the "
            "password becomes unusable and a reset link is e-mailed) or `both`; the person must then set "
            "their own password. The caller's own account → 409 `conflict` (use `auth/change-password/`); a "
            "superuser's account with `temporary_password` or `both`, unless the caller is a superuser too → "
            "409 `conflict` (send `email_link`)."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=StaffResetPasswordSerializer,
        responses={200: StaffCredentialsSerializer, **ADMIN_ERRORS},
    )
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

    @extend_schema(
        operation_id="admin_staff_send_password_link",
        description=(
            f"{STAFF_ID_DESCRIPTION} E-mails the account a new single-use link to set its password (invite "
            "for an account that never signed in nor set one, else reset); the password and the sessions are "
            "not changed, earlier links are revoked. The caller's own account, an inactive account, or "
            "e-mail links not available → 409 `conflict`; 5/hour per manager and account."
        ),
        parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: StaffLinkSentSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def send_password_link(self, request: Request, pk: int) -> Response:
        user = self._target(int(pk))
        token = services.send_password_link(user, actor=cast(StaffUser, request.user), request=request)
        return Response({"staff": staff_member_payload(user.pk), "activation": activation_payload(token)})

    @extend_schema(
        operation_id="admin_staff_reset_mfa",
        description=(
            f"{STAFF_ID_DESCRIPTION} A lost phone: removes the account's admin panel MFA (TOTP device and "
            "recovery codes; ops console devices stay) and revokes every open session (`admin_revoked`). "
            "The caller's own account, an account without panel MFA, or a superuser's account unless the "
            "caller is a superuser too (`manage.py resetstaffmfa` stays the break-glass path) → 409 "
            "`conflict`. Optional body "
            "`reason` (trimmed, at most 200 characters, blank allowed) is kept on the audit entry. Audited "
            "`auth.mfa_reset` and alerted."
        ),
        parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=StaffResetMfaSerializer,
        responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
    )
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
    "STAFF_ID_DESCRIPTION",
    "StaffViewSet",
]
