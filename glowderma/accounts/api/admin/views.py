import hashlib
import uuid
from collections.abc import Sequence
from typing import Any, cast

from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema
from rest_framework import mixins
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import ErrorDetail, NotFound, ValidationError
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

from glowderma.accounts import mfa, selectors, services, tokens
from glowderma.accounts.api.admin.serializers import (
    AccessSerializer,
    ChangePasswordSerializer,
    LoginResponseSerializer,
    LoginSerializer,
    LogoutSerializer,
    MeSerializer,
    MeUpdateSerializer,
    MfaChallengeSerializer,
    MfaConfirmSerializer,
    MfaDisableSerializer,
    MfaPasswordSerializer,
    MfaRecoveryCodesSerializer,
    MfaRegenerateSerializer,
    MfaSetupSerializer,
    MfaStatusSerializer,
    MfaVerifySerializer,
    OAuthCallbackSerializer,
    OwnPasswordLinkSerializer,
    PasswordTokenInfoSerializer,
    PasswordTokenSerializer,
    PasswordTokenSetSerializer,
    RefreshSerializer,
    StaffCreateSerializer,
    StaffCredentialsSerializer,
    StaffLinkSentSerializer,
    StaffListItemSerializer,
    StaffListQuerySerializer,
    StaffMemberSerializer,
    StaffResetMfaSerializer,
    StaffResetPasswordSerializer,
    StaffUpdateSerializer,
    validated_new_password,
)
from glowderma.accounts.api.base import AdminGenericViewSet, WorkforceAPIView
from glowderma.accounts.api.throttles import (
    AuthClientIPThrottle,
    LoginRateThrottle,
    MfaVerifyIPThrottle,
    OAuthIPThrottle,
    PasswordLinkThrottle,
    PasswordTokenIPThrottle,
    RefreshRateThrottle,
    StaffRateThrottle,
)
from glowderma.accounts.models import AuthSession, StaffPasswordToken, StaffUser
from glowderma.accounts.permissions import IsManager
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import AuthFailed, OAuthExchangeFailed, ProviderUnavailable
from glowderma.core.api.parsers import JSONParser
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
    PUBLIC_ERRORS,
    ErrorEnvelopeSerializer,
)
from glowderma.core.idempotency import idempotent
from glowderma.core.pii import mask_email
from glowderma.core.uzsource import N_
from glowderma.integrations.oauth.ports import OAuthExchangeError, SocialClaims
from glowderma.integrations.oauth.registry import PROVIDER_NAMES, get_oauth_provider

ERRORS = {400: ErrorEnvelopeSerializer, 401: ErrorEnvelopeSerializer, 429: ErrorEnvelopeSerializer}
TEMPORARY_PASSWORD = "temporary_password"  # noqa: S105 - the response key, not a password
PROVIDER_PARAM = OpenApiParameter(
    name="provider", type=OpenApiTypes.STR, location=OpenApiParameter.PATH, enum=list(PROVIDER_NAMES)
)


def _me(user: StaffUser, session: AuthSession) -> dict[str, Any]:
    return {
        "id": user.pk,
        "email": user.email,
        "display_name": user.full_name or user.email,
        "role": user.role,
        "is_superuser": user.is_superuser,
        "is_manager": user.is_manager or user.is_superuser,
        "linked_providers": list(
            user.social_identities.order_by("provider").values_list("provider", flat=True)
        ),
        "session_expires_at": timeutils.isoformat(session.absolute_expires_at),
        # how the current session's sign-in was proven (contract §2.1); null for a session from before 0007
        "auth_method": session.auth_method or None,
        # feat/admin-ux: the forced change gate, the panel language, the TOTP state and what the server offers
        "must_change_password": user.must_change_password,
        "language": user.language,
        "mfa_enabled": mfa.enabled(user),
        "capabilities": {
            "staff_email_invite": services.staff_email_invite_available(),
        },
    }


def _activation(token: StaffPasswordToken | None) -> dict[str, Any] | None:
    if token is None:
        return None
    return {"sent": True, "expires_at": timeutils.isoformat(token.expires_at)}


def _open_link(user: StaffUser) -> StaffPasswordToken | None:
    """The link a create or reset just issued (the newest open one), for the answer's ``activation``."""
    return (
        StaffPasswordToken.objects.filter(staff=user, used_at__isnull=True, revoked_at__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )


def _mfa_challenge(challenge: mfa.Challenge) -> dict[str, Any]:
    return {
        "mfa_required": True,
        "challenge": challenge.raw,
        "challenge_expires_at": timeutils.isoformat(challenge.expires_at),
    }


# The 200 of a sign-in: a session, or (admin panel MFA on) the challenge for auth/mfa/verify/.
LOGIN_RESULT = PolymorphicProxySerializer(
    component_name="AdminLoginResult",
    serializers=[LoginResponseSerializer, MfaChallengeSerializer],
    resource_type_field_name=None,
)


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
            return Response(_mfa_challenge(challenge))
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


class MeView(WorkforceAPIView):
    allow_password_change_pending = True

    @extend_schema(operation_id="admin_auth_me", responses={200: MeSerializer, **ERRORS})
    def get(self, request: Request) -> Response:
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(_me(cast(StaffUser, request.user), session))

    @extend_schema(
        operation_id="admin_auth_me_update",
        description="Change the caller's admin panel language (`uz`, `ru`, `en`); returns Me. Allowed "
        "while a password change is pending.",
        request=MeUpdateSerializer,
        responses={200: MeSerializer, **ADMIN_ERRORS},
    )
    def patch(self, request: Request) -> Response:
        body = MeUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user = cast(StaffUser, request.user)
        services.set_staff_language(user, body.validated_data["language"])
        session = AuthSession.objects.get(pk=cast(dict[str, Any], request.auth)["sid"])
        return Response(_me(user, session))


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
        return Response({"email_masked": mask_email(user.email), "activation": _activation(token)})


# --- admin panel MFA (feat/admin-ux, contract §2.1 "MFA"; glowderma.accounts.mfa) ---------------------------


def _status(user: StaffUser) -> dict[str, Any]:
    current = mfa.status(user)
    return {"enabled": current.enabled, "recovery_codes_left": current.recovery_codes_left}


def _session_id(request: Request) -> Any:
    return cast(dict[str, Any], request.auth)["sid"]


class MfaStatusView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_status",
        description="The caller's admin panel MFA: on or off, and the unused recovery codes.",
        responses={200: MfaStatusSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(_status(cast(StaffUser, request.user)))


class MfaSetupView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_setup",
        description="Start enabling the admin panel MFA (re-enter the current password): a new pending "
        "TOTP device (an earlier pending one is replaced) as an `otpauth://` URI for the QR code and a "
        "manual key. Wrong password → 400 `password_incorrect` on `current_password` (the "
        "change-password lockout); already on → 409 `conflict`.",
        request=MfaPasswordSerializer,
        responses={200: MfaSetupSerializer, **ADMIN_ERRORS},
    )
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaPasswordSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        setup = mfa.begin_setup(user)
        return Response(
            {"device_id": setup.device_id, "otpauth_uri": setup.otpauth_uri, "manual_key": setup.manual_key}
        )


class MfaConfirmView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_confirm",
        description="Confirm the pending device with its first code: MFA is on, the current session counts "
        "as verified, and the ten recovery codes are returned once. Wrong code or not the caller's pending "
        "device → 400 `mfa_code_invalid`; already on → 409; 429 while the code lockout runs.",
        request=MfaConfirmSerializer,
        responses={200: MfaRecoveryCodesSerializer, **ADMIN_ERRORS},
    )
    def post(self, request: Request) -> Response:
        body = MfaConfirmSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        codes = mfa.confirm_setup(
            cast(StaffUser, request.user),
            body.validated_data["device_id"],
            body.validated_data["code"],
            session_id=_session_id(request),
            request=request,
        )
        return Response({"recovery_codes": codes})


class MfaDisableView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_disable",
        description="Turn the admin panel MFA off: the current password plus a code or a recovery code. "
        "Wrong password → 400 `password_incorrect`; wrong code → 400 `mfa_code_invalid`; off → 409.",
        request=MfaDisableSerializer,
        responses={200: MfaStatusSerializer, **ADMIN_ERRORS},
    )
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaDisableSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        mfa.disable(user, code_or_recovery=body.validated_data["code_or_recovery"], request=request)
        return Response(_status(user))


class MfaRecoveryCodesView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_recovery_codes",
        description="Ten new recovery codes (the old ones stop working), returned once: the current password "
        "plus a TOTP code. Errors as `auth/mfa/disable/`.",
        request=MfaRegenerateSerializer,
        responses={200: MfaRecoveryCodesSerializer, **ADMIN_ERRORS},
    )
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaRegenerateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        codes = mfa.regenerate_recovery_codes(user, code=body.validated_data["code"], request=request)
        return Response({"recovery_codes": codes})


class MfaVerifyView(PublicAuthAPIView):
    throttle_classes = [MfaVerifyIPThrottle]

    @extend_schema(
        operation_id="admin_auth_mfa_verify",
        description="Public (no bearer): the sign-in's second step. The challenge of `auth/login/` or "
        "`auth/oauth/{provider}/` plus exactly one of `code` / `recovery_code` → the normal sign-in answer "
        "(session `auth_method` = the first step's). Wrong code → 400 `mfa_code_invalid` (5 attempts per "
        "challenge); unknown, expired (5 min), used or exhausted challenge, or a password change / "
        "deactivation / MFA reset since → 400 `mfa_challenge_invalid` (sign in again); 429 while the "
        "account's code lockout runs or for the per-IP limit.",
        auth=[],
        request=MfaVerifySerializer,
        responses={200: LoginResponseSerializer, **ERRORS},
    )
    def post(self, request: Request) -> Response:
        body = MfaVerifySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        session, refresh = services.complete_mfa_login(
            data["challenge"],
            code=data.get("code") or None,
            recovery_code=data.get("recovery_code") or None,
            device_label=request.META.get("HTTP_USER_AGENT", "")[:100],
            request=request,
        )
        user = cast(StaffUser, session.staff)
        return Response({**_access(session), "refresh": refresh, "user": _me(user, session)})


# A PKCE verifier is single use across every API instance (admin contract §2.1): the key outlives the
# provider's code (10 min) plus a minute of clock skew.
VERIFIER_TTL_SECONDS = 660


def claim_code_verifier(code_verifier: str) -> bool:
    """Atomically mark ``code_verifier`` used (``cache.add`` on its SHA-256, shared Redis): ``False`` when
    another request (any worker, any instance) already presented it."""
    digest = hashlib.sha256(code_verifier.encode("utf-8")).hexdigest()
    return bool(cache.add(f"oauth:verifier:{digest}", 1, VERIFIER_TTL_SECONDS))


def _exchange(request: Request, provider: str) -> SocialClaims:
    """Validate the callback body and turn the code into verified claims (provider errors -> API errors).

    A reused ``code_verifier`` is refused with the uniform 401 ``social_account_not_allowed`` before the
    provider is called, so a replayed callback (a second tab, a leaked request) cannot sign in twice even when
    the provider would still accept the code.
    """
    oauth = get_oauth_provider(provider)  # 404 for an unknown provider, before the body is read
    serializer = OAuthCallbackSerializer(data=request.data, context={"provider": provider})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    if not claim_code_verifier(data["code_verifier"]):
        raise AuthFailed("social_account_not_allowed")
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


class OAuthProviderView(WorkforceAPIView):
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
            "auth/login/ (with the admin panel MFA on: the challenge for auth/mfa/verify/)."
        ),
        auth=[],
        parameters=[PROVIDER_PARAM],
        request=OAuthCallbackSerializer,
        responses={
            200: LOGIN_RESULT,
            **PUBLIC_ERRORS,
            404: ErrorEnvelopeSerializer,
            503: ErrorEnvelopeSerializer,
        },
    )
    def post(self, request: Request, provider: str) -> Response:
        claims = _exchange(request, provider)
        try:
            session, refresh = services.social_login(
                provider, claims, device_label=request.META.get("HTTP_USER_AGENT", "")[:100], request=request
            )
        except mfa.MfaRequired as required:
            return Response(_mfa_challenge(required.challenge))
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


class OAuthLinkView(WorkforceAPIView):
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
        get_oauth_provider(provider)  # 404 first (unknown provider, fake OAuth under the production settings)
        sid = cast(dict[str, Any], request.auth)["sid"]
        session = services.require_password_session(sid)  # 403 before the provider is called
        claims = _exchange(request, provider)
        user = cast(StaffUser, request.user)
        services.link_identity(user, provider, claims, session_id=sid, request=request)
        return Response(_me(user, session))


# --- staff (contract §2.16) -------------------------------------------------------------------------------

STAFF_ID_DESCRIPTION = "Manager only (403 `permission_denied` otherwise)."


def _member(staff_id: int) -> dict[str, Any]:
    """The ``StaffMember`` of a fresh read (every write answers with it)."""
    user = selectors.staff_member(staff_id)
    if user is None:  # pragma: no cover - the write just locked the row
        raise NotFound
    return dict(StaffMemberSerializer(user).data)


def staff_credentials(user: StaffUser, password: str | None, delivery: str) -> dict[str, Any]:
    link = _open_link(user) if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD else None
    return {"staff": _member(user.pk), TEMPORARY_PASSWORD: password, "activation": _activation(link)}


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
        return Response(_member(user.pk))

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
        return Response(_member(user.pk))

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
        return Response(_member(user.pk))

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
        link = _open_link(user) if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD else None
        return Response(
            {"staff": _member(user.pk), TEMPORARY_PASSWORD: password, "activation": _activation(link)}
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
        return Response({"staff": _member(user.pk), "activation": _activation(token)})

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
        return Response(_member(user.pk))
