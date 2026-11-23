"""Staff sign-in sessions: password login (with the MFA challenge), refresh and logout."""

from __future__ import annotations

from typing import cast

from django.contrib.auth import authenticate
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import mfa, services
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
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import audit
from glowderma.core.api.exceptions import AuthFailed


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
        email = serializer.validated_data["email"]
        hashed = email_hash(email)
        guard = LoginGuard()
        guard.check("login", hashed)
        user = authenticate(request=request, email=email, password=serializer.validated_data["password"])
        if not isinstance(user, StaffUser) or not user.is_active:
            guard.record_failure("login", hashed)
            audit.record(
                action="auth.login_failed",
                entity_type="staff",
                entity_id="",
                request=request,
                after={"email": email, "method": "password"},
            )
            raise AuthFailed("invalid_credentials")
        guard.reset("login", hashed)
        if mfa.enabled(user):
            challenge = mfa.issue_challenge(user, AuthSession.AuthMethod.PASSWORD, request=request)
            return Response(mfa_challenge_payload(challenge))
        with transaction.atomic():
            session, refresh = services.start_staff_session(
                user, device_label=request.META.get("HTTP_USER_AGENT", "")[:100]
            )
            audit.record(
                action="auth.login_succeeded",
                entity_type="staff",
                entity_id=str(user.pk),
                actor=user,
                request=request,
                after={"email": email, "method": "password"},
            )
        return Response({**access_payload(session), "refresh": refresh, "user": me_payload(user, session)})


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
        try:
            session = services.refresh_staff_session(serializer.validated_data["refresh"])
        except AuthFailed as exc:
            raise AuthFailed("SESSION_EXPIRED") from exc
        scope = serializer.validated_data["scope"]
        user = cast(StaffUser, session.staff)
        with transaction.atomic():
            if scope == "current":
                services.revoke_session(session, "logout")
                count = 1
            else:
                count = services.revoke_user_sessions(
                    user, "logout", except_sid=session.pk if scope == "others" else None
                )
            audit.record(
                action="auth.logout",
                entity_type="staff",
                entity_id=str(user.pk),
                actor=user,
                request=request,
                after={"scope": scope, "revoked_count": count},
            )
        return Response(status=204)


__all__ = [
    "LoginView",
    "LogoutView",
    "RefreshView",
]
