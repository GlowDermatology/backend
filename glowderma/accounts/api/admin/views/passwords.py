"""Staff passwords: changing one's own, and the e-mailed set-password links and their tokens."""

from __future__ import annotations

import uuid
from typing import Any, cast

from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services
from glowderma.accounts.api.admin.payloads import activation_payload
from glowderma.accounts.api.admin.schemas.passwords import (
    CHANGE_PASSWORD_POST,
    OWN_PASSWORD_LINK_POST,
    PASSWORD_TOKEN_CHECK_POST,
    PASSWORD_TOKEN_SET_POST,
)
from glowderma.accounts.api.admin.serializers import (
    ChangePasswordSerializer,
    PasswordTokenSerializer,
    PasswordTokenSetSerializer,
    validated_new_password,
)
from glowderma.accounts.api.base import PublicAuthAPIView, WorkforceAPIView
from glowderma.accounts.api.throttles import (
    PasswordLinkThrottle,
    PasswordTokenIPThrottle,
    StaffRateThrottle,
)
from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils
from glowderma.core.api.parsers import JSONParser
from glowderma.core.pii import mask_email


class ChangePasswordView(WorkforceAPIView):
    parser_classes = [JSONParser]
    allow_password_change_pending = True

    @CHANGE_PASSWORD_POST
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        services.check_password_change_lockout(user)
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        services.change_own_password(
            user,
            current_password=serializer.validated_data["current_password"],
            new_password=serializer.validated_data["new_password"],
            keep_session_id=uuid.UUID(cast(dict[str, Any], request.auth)["sid"]),
            request=request,
        )
        return Response(status=204)


class PasswordTokenCheckView(PublicAuthAPIView):
    throttle_classes = [PasswordTokenIPThrottle]

    @PASSWORD_TOKEN_CHECK_POST
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

    @PASSWORD_TOKEN_SET_POST
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

    @OWN_PASSWORD_LINK_POST
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
