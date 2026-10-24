import uuid
from typing import Any, cast

from django.contrib.auth import authenticate
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from glowderma.accounts import services, tokens
from glowderma.accounts.api.admin.serializers import (
    AccessSerializer,
    ChangePasswordSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    LogoutSerializer,
    MeSerializer,
    RefreshSerializer,
)
from glowderma.accounts.api.base import AdminAPIView
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.api.throttles import AuthClientIPThrottle, LoginRateThrottle, RefreshRateThrottle
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import AuthFailed
from glowderma.core.api.schema import ErrorEnvelopeSerializer

ERRORS = {400: ErrorEnvelopeSerializer, 401: ErrorEnvelopeSerializer, 429: ErrorEnvelopeSerializer}


def _me(user: StaffUser, session: AuthSession) -> dict[str, Any]:
    return {
        "id": user.pk,
        "email": user.email,
        "display_name": user.full_name or user.email,
        "is_superuser": user.is_superuser,
        "linked_providers": list(
            user.social_identities.order_by("provider").values_list("provider", flat=True)
        ),
        "session_expires_at": timeutils.isoformat(session.absolute_expires_at),
    }


def _access(session: AuthSession) -> dict[str, Any]:
    access, expires = tokens.issue_access(session)
    return {
        "access": access,
        "access_expires_at": timeutils.isoformat(expires),
        "refresh_expires_at": timeutils.isoformat(session.absolute_expires_at),
    }


class PublicAuthAPIView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []


class LoginView(PublicAuthAPIView):
    throttle_classes = [LoginRateThrottle]

    @extend_schema(
        operation_id="admin_auth_login",
        auth=[],
        request=LoginSerializer,
        responses={200: LoginResponseSerializer, **ERRORS},
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
        return Response({**_access(session), "refresh": refresh, "user": _me(user, session)})


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
        return Response(_access(session))


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


class MeView(AdminAPIView):
    @extend_schema(operation_id="admin_auth_me", responses={200: MeSerializer, **ERRORS})
    def get(self, request: Request) -> Response:
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(_me(cast(StaffUser, request.user), session))


class ChangePasswordView(AdminAPIView):
    parser_classes = [JSONParser]

    @extend_schema(
        operation_id="admin_auth_change_password",
        request=ChangePasswordSerializer,
        responses={204: None, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        guard = LoginGuard()
        hashed = email_hash(cast(StaffUser, request.user).email)
        guard.check("change_password", hashed)
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = StaffUser.objects.select_for_update().get(pk=cast(StaffUser, request.user).pk)
            if not user.check_password(serializer.validated_data["current_password"]):
                guard.record_failure("change_password", hashed)
                raise ValidationError(
                    {"current_password": [ErrorDetail("Joriy parol noto‘g‘ri.", code="password_incorrect")]}
                )
            if user.check_password(serializer.validated_data["new_password"]):
                raise ValidationError(
                    {
                        "new_password": [
                            ErrorDetail("Yangi parol eskisidan farq qilsin.", code="password_unchanged")
                        ]
                    }
                )
            user.set_password(serializer.validated_data["new_password"])
            user.save(update_fields=["password"])
            services.revoke_user_sessions(
                user, "password_changed", except_sid=uuid.UUID(cast(dict[str, Any], request.auth)["sid"])
            )
            audit.record(
                action="auth.password_changed",
                entity_type="staff",
                entity_id=str(user.pk),
                actor=user,
                request=request,
            )
        guard.reset("change_password", hashed)
        return Response(status=204)
