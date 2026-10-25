import uuid
from collections.abc import Sequence
from typing import Any, cast

from django.contrib.auth import authenticate
from django.db import transaction
from django.http import HttpRequest
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import ErrorDetail, NotFound, ValidationError
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

from glowderma.accounts import services, tokens
from glowderma.accounts.api.admin.serializers import (
    AccessSerializer,
    ChangePasswordSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    LogoutSerializer,
    MeSerializer,
    OAuthCallbackSerializer,
    RefreshSerializer,
)
from glowderma.accounts.api.base import AdminAPIView
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.api.throttles import (
    AuthClientIPThrottle,
    LoginRateThrottle,
    OAuthIPThrottle,
    RefreshRateThrottle,
)
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import AuthFailed, OAuthExchangeFailed, ProviderUnavailable
from glowderma.core.api.parsers import JSONParser
from glowderma.core.api.schema import ADMIN_ERRORS, PUBLIC_ERRORS, ErrorEnvelopeSerializer
from glowderma.integrations.oauth.ports import OAuthExchangeError, SocialClaims
from glowderma.integrations.oauth.registry import PROVIDER_NAMES, get_oauth_provider

ERRORS = {400: ErrorEnvelopeSerializer, 401: ErrorEnvelopeSerializer, 429: ErrorEnvelopeSerializer}
PROVIDER_PARAM = OpenApiParameter(
    name="provider", type=OpenApiTypes.STR, location=OpenApiParameter.PATH, enum=list(PROVIDER_NAMES)
)


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
        guard.check("change_password", hashed, reserve=False)
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        guard.check("change_password", hashed)
        with transaction.atomic():
            user = StaffUser.objects.select_for_update().get(pk=cast(StaffUser, request.user).pk)
            if not user.check_password(serializer.validated_data["current_password"]):
                guard.record_failure("change_password", hashed)
                raise ValidationError(
                    {"current_password": [ErrorDetail("Joriy parol noto‘g‘ri.", code="password_incorrect")]}
                )
            if user.check_password(serializer.validated_data["new_password"]):
                # Current credentials were verified; this validation error is not a failed guess.
                guard.reset("change_password", hashed)
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


def _exchange(request: Request, provider: str) -> SocialClaims:
    """Validate the callback body and turn the code into verified claims (provider errors -> API errors)."""
    oauth = get_oauth_provider(provider)  # 404 for an unknown provider, before the body is read
    serializer = OAuthCallbackSerializer(data=request.data, context={"provider": provider})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        return oauth.exchange(
            code=data["code"],
            code_verifier=data["code_verifier"],
            redirect_uri=data["redirect_uri"],
            nonce=data.get("nonce"),
        )
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise ProviderUnavailable from exc
        raise OAuthExchangeFailed from exc


class OAuthProviderView(AdminAPIView):
    """``auth/oauth/{provider}/``: POST signs in without a bearer (public, per-IP throttle), DELETE unlinks
    the caller's identity and is an ordinary admin call. The two methods share one route, so authentication,
    permission and throttling are chosen per request method."""

    public = False  # set per request in initialize_request; False also covers schema generation

    def initialize_request(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Request:
        self.public = request.method == "POST"
        return super().initialize_request(request, *args, **kwargs)

    def get_authenticators(self) -> list[BaseAuthentication]:
        return [] if self.public else super().get_authenticators()

    def get_permissions(self) -> Sequence[BasePermission]:
        return [AllowAny()] if self.public else cast(Sequence[BasePermission], super().get_permissions())

    def get_throttles(self) -> list[BaseThrottle]:
        return [OAuthIPThrottle()] if self.public else super().get_throttles()

    @extend_schema(
        operation_id="admin_auth_oauth_login",
        description=(
            "Staff social sign-in, allow-list only: Django exchanges the authorization code with the "
            "provider and signs in the staff member whose identity is already linked. Same response as "
            "auth/login/."
        ),
        auth=[],
        parameters=[PROVIDER_PARAM],
        request=OAuthCallbackSerializer,
        responses={
            200: LoginResponseSerializer,
            **PUBLIC_ERRORS,
            404: ErrorEnvelopeSerializer,
            503: ErrorEnvelopeSerializer,
        },
    )
    def post(self, request: Request, provider: str) -> Response:
        claims = _exchange(request, provider)
        session, refresh = services.social_login(
            provider, claims, device_label=request.META.get("HTTP_USER_AGENT", "")[:100], request=request
        )
        user = cast(StaffUser, session.staff)
        return Response({**_access(session), "refresh": refresh, "user": _me(user, session)})

    @extend_schema(
        operation_id="admin_auth_oauth_unlink",
        description="Unlink the caller's identity for the provider.",
        parameters=[PROVIDER_PARAM],
        responses={204: None, **ADMIN_ERRORS},
    )
    def delete(self, request: Request, provider: str) -> Response:
        if provider not in PROVIDER_NAMES:
            raise NotFound
        services.unlink_identity(cast(StaffUser, request.user), provider, request=request)
        return Response(status=204)


class OAuthLinkView(AdminAPIView):
    @extend_schema(
        operation_id="admin_auth_oauth_link",
        description=(
            "Link the provider identity to the signed-in staff member (same body as the sign-in); returns Me "
            "with the updated linked_providers."
        ),
        parameters=[PROVIDER_PARAM],
        request=OAuthCallbackSerializer,
        responses={200: MeSerializer, **ADMIN_ERRORS, 503: ErrorEnvelopeSerializer},
    )
    def post(self, request: Request, provider: str) -> Response:
        claims = _exchange(request, provider)
        user = cast(StaffUser, request.user)
        services.link_identity(user, provider, claims, request=request)
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(_me(user, session))
