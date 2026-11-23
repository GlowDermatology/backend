"""Staff passwords: changing one's own, and the e-mailed set-password links and their tokens."""

from __future__ import annotations

import uuid
from typing import Any, cast

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services
from glowderma.accounts.api.admin.payloads import activation_payload
from glowderma.accounts.api.admin.serializers import (
    ChangePasswordSerializer,
    OwnPasswordLinkSerializer,
    PasswordTokenInfoSerializer,
    PasswordTokenSerializer,
    PasswordTokenSetSerializer,
    validated_new_password,
)
from glowderma.accounts.api.admin.views.schema import ERRORS
from glowderma.accounts.api.base import PublicAuthAPIView, WorkforceAPIView
from glowderma.accounts.api.throttles import (
    PasswordLinkThrottle,
    PasswordTokenIPThrottle,
    StaffRateThrottle,
)
from glowderma.accounts.models import StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import audit, timeutils
from glowderma.core.api.parsers import JSONParser
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)
from glowderma.core.pii import mask_email
from glowderma.core.uzsource import N_


class ChangePasswordView(WorkforceAPIView):
    parser_classes = [JSONParser]
    allow_password_change_pending = True

    @extend_schema(
        operation_id="admin_auth_change_password",
        request=ChangePasswordSerializer,
        responses={204: None, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        guard = LoginGuard()
        hashed = email_hash(cast(StaffUser, request.user).email)
        guard.check("change_password", hashed, reserve=False)
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        guard.check("change_password", hashed)
        with transaction.atomic():
            user = StaffUser.objects.select_for_update().get(pk=cast(StaffUser, request.user).pk)
            if not user.check_password(serializer.validated_data["current_password"]):
                guard.record_failure("change_password", hashed)
                raise ValidationError(
                    {
                        "current_password": [
                            ErrorDetail(N_("Joriy parol noto‘g‘ri."), code="password_incorrect")
                        ]
                    }
                )
            if user.check_password(serializer.validated_data["new_password"]):
                # Current credentials were verified; this validation error is not a failed guess.
                guard.reset("change_password", hashed)
                raise ValidationError(
                    {
                        "new_password": [
                            ErrorDetail(N_("Yangi parol eskisidan farq qilsin."), code="password_unchanged")
                        ]
                    }
                )
            user.set_password(serializer.validated_data["new_password"])
            forced = services.record_own_password_change(user)
            services.revoke_user_sessions(
                user, "password_changed", except_sid=uuid.UUID(cast(dict[str, Any], request.auth)["sid"])
            )
            audit.record(
                action="auth.password_changed",
                entity_type="staff",
                entity_id=str(user.pk),
                actor=user,
                request=request,
                after={"forced": forced},
            )
        guard.reset("change_password", hashed)
        return Response(status=204)


class PasswordTokenCheckView(PublicAuthAPIView):
    throttle_classes = [PasswordTokenIPThrottle]

    @extend_schema(
        operation_id="admin_auth_password_token_check",
        description="Public (no bearer): whose password link this is, for the set-password page. An unknown, "
        "used, revoked or expired link, or one of an inactive account → one uniform 400 `token_invalid`.",
        auth=[],
        request=PasswordTokenSerializer,
        responses={200: PasswordTokenInfoSerializer, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        body = PasswordTokenSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        token = services.password_token(body.validated_data["token"])
        return Response(
            {
                "email_masked": mask_email(token.staff.email),
                "full_name": token.staff.full_name,
                "purpose": token.purpose,
                "expires_at": timeutils.isoformat(token.expires_at),
            }
        )


class PasswordTokenSetView(PublicAuthAPIView):
    throttle_classes = [PasswordTokenIPThrottle]

    @extend_schema(
        operation_id="admin_auth_password_token_set",
        description="Public (no bearer): set the password with a link (single use). The password is "
        "validated for the link's account (the change-password codes, `password_unchanged` for the current "
        "one); every session of the account is revoked and no new one is opened — the person signs in next. "
        "A bad link → 400 `token_invalid`.",
        auth=[],
        request=PasswordTokenSetSerializer,
        responses={204: None, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        body = PasswordTokenSetSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        token = services.password_token(body.validated_data["token"])
        try:
            password = validated_new_password(body.validated_data["new_password"], token.staff)
        except ValidationError as exc:
            raise ValidationError({"new_password": exc.detail}) from exc
        services.set_password_with_token(body.validated_data["token"], password, request=request)
        return Response(status=204)


class OwnPasswordLinkView(WorkforceAPIView):
    allow_password_change_pending = True
    throttle_classes = [StaffRateThrottle, PasswordLinkThrottle]

    @extend_schema(
        operation_id="admin_auth_password_link",
        description="E-mail the caller a single-use link to set their password, for an account that must "
        "change it but cannot type the current one (signed in with a social identity, an e-mail-link "
        "account). 409 `conflict` when no change is pending or e-mail links are not available "
        "(`capabilities.staff_email_invite`); 5/hour.",
        request=None,
        responses={200: OwnPasswordLinkSerializer, **ADMIN_ERRORS},
    )
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        token = services.request_own_password_link(user, request=request)
        return Response({"email_masked": mask_email(user.email), "activation": activation_payload(token)})


__all__ = [
    "ChangePasswordView",
    "OwnPasswordLinkView",
    "PasswordTokenCheckView",
    "PasswordTokenSetView",
]
