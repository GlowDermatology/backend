"""Staff sign-in sessions: password login (with the MFA challenge), refresh and logout."""

from __future__ import annotations

from typing import cast

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services
from glowderma.accounts.api.admin.payloads import access_payload, me_payload, mfa_challenge_payload
from glowderma.accounts.api.admin.serializers import (
    AccessSerializer,
    LoginSerializer,
    LogoutSerializer,
    RefreshSerializer,
)
from glowderma.accounts.api.admin.views.schema import ERRORS, LOGIN_RESULT
from glowderma.accounts.api.base import PublicAuthAPIView
from glowderma.accounts.api.throttles import (
    AuthClientIPThrottle,
    LoginRateThrottle,
    RefreshRateThrottle,
)
from glowderma.accounts.models import AuthSession


class LoginView(PublicAuthAPIView):
    throttle_classes = [LoginRateThrottle]

    @extend_schema(
        operation_id="admin_auth_login",
        description="Email + password sign-in. With the admin panel MFA on (feat/admin-ux) a correct "
        "password opens no session: the answer is `{mfa_required: true, challenge, challenge_expires_at}` "
        "and `auth/mfa/verify/` opens it.",
        auth=[],
        request=LoginSerializer,
        responses={200: LOGIN_RESULT, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        signed_in = services.password_sign_in(
            serializer.validated_data["email"], serializer.validated_data["password"], request=request
        )
        if signed_in.challenge is not None:
            return Response(mfa_challenge_payload(signed_in.challenge))
        session = cast(AuthSession, signed_in.session)
        body = {
            **access_payload(session),
            "refresh": signed_in.refresh,
            "user": me_payload(signed_in.user, session),
        }
        return Response(body)


class RefreshView(PublicAuthAPIView):
    throttle_classes = [AuthClientIPThrottle, RefreshRateThrottle]

    @extend_schema(
        operation_id="admin_auth_refresh",
        auth=[],
        request=RefreshSerializer,
        responses={200: AccessSerializer, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = services.refresh_staff_session(serializer.validated_data["refresh"])
        return Response(access_payload(session))


class LogoutView(PublicAuthAPIView):
    throttle_classes = [AuthClientIPThrottle, RefreshRateThrottle]

    @extend_schema(
        operation_id="admin_auth_logout", auth=[], request=LogoutSerializer, responses={204: None, **ERRORS}
    )
    def post(self, request: Request) -> Response:
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.sign_out_staff(
            serializer.validated_data["refresh"], serializer.validated_data["scope"], request=request
        )
        return Response(status=204)


__all__ = [
    "LoginView",
    "LogoutView",
    "RefreshView",
]
