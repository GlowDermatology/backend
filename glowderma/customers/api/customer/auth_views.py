"""``auth/*`` of the customer API (customer contract §2.1): OTP, registration, login with the new-device code,
reset, rotating refresh, logout and social sign-in (Google, Facebook, Apple)."""

from __future__ import annotations

from typing import Any, cast

from django.conf import settings
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts import sessions as account_sessions
from glowderma.accounts import tokens
from glowderma.accounts.api.customer_base import CustomerAuthAPIView
from glowderma.accounts.api.throttles import (
    AuthClientIPThrottle,
    RefreshRateThrottle,
    RefreshUnknownThrottle,
    SmsDeviceThrottle,
    SmsIPThrottle,
    SmsPhoneThrottle,
)
from glowderma.accounts.authentication import DeviceHeaders
from glowderma.accounts.models import Customer
from glowderma.core import enums, i18n, integration_modes, timeutils
from glowderma.core.api.exceptions import OtpInvalid, TermsNotPublished, customer_error_body
from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    DEVICE_KEY_HEADER,
)
from glowderma.core.pii import mask_email, mask_phone
from glowderma.customers import selectors, services
from glowderma.customers.api.customer import serializers as s
from glowderma.integrations.oauth.registry import CUSTOMER_PROVIDERS, customer_provider_available
from glowderma.site_config import selectors as site_config_selectors

AUTH_PARAMETERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]
# ``auth/login/`` and ``auth/social/`` answer either AuthTokens or a NextStep (customer contract §2.0).
AUTH_TOKENS_OR_NEXT_STEP = PolymorphicProxySerializer(
    component_name="AuthTokensOrNextStep",
    serializers=[s.AuthTokensSerializer, s.NextStepSerializer],
    resource_type_field_name=None,
)


def client_ip(request: Request) -> str:
    return str(getattr(request, "client_ip", "") or request.META.get("REMOTE_ADDR", ""))


def device_context(view: Any, request: Request) -> services.DeviceContext:
    device = cast(DeviceHeaders, view.device)
    return services.DeviceContext(
        fid=device.fid,
        platform=device.platform,
        app_version=device.app_version,
        ip=client_ip(request),
        lang=view.language,
    )


def auth_tokens(sign_in: services.SignIn, *, fid: str, request: Request) -> dict[str, Any]:
    """``AuthTokens`` (customer contract §2.0); records the new session's client (user agent, IP, location)
    for the session list (B-W1, §2.2.10)."""
    session = sign_in.started.session
    platform = session.device.platform if session.device is not None else ""
    user_agent, ip = account_sessions.activity_facts(request, platform=platform)
    account_sessions.stamp_session(session.pk, user_agent=user_agent, ip=ip)
    access, expires = tokens.issue_access(session)
    customer = Customer.objects.get(pk=sign_in.customer_id)
    return {
        "access": access,
        "access_expires_at": _iso(expires),
        "refresh": sign_in.started.raw_refresh,
        "refresh_expires_at": _iso(session.absolute_expires_at),
        "device_key": sign_in.started.raw_device_key,
        "customer": selectors.me_payload(customer, fid=fid),
        "is_new_customer": sign_in.is_new_customer,
        "reactivated": sign_in.reactivated,
    }


def _iso(value: Any) -> str:
    return timeutils.isoformat(value)


def code_issue_body(issue: account_services.CodeIssue) -> dict[str, Any]:
    return {
        "phone_mask": mask_phone(issue.phone) if issue.phone else None,
        "email_mask": mask_email(issue.email) if issue.email else None,
        "purpose": issue.purpose,
        "expires_in": issue.expires_in,
        "resend_in": issue.resend_in,
        "attempts": issue.attempts,
        "length": issue.length,
    }


def device_step_body(step: services.DeviceStep) -> dict[str, Any]:
    issue = step.issue
    return {
        "next_step": enums.NextStep.VERIFY_DEVICE.value,
        # the code went where the customer signed in with (hybrid identity): one mask, the other null
        "phone_mask": mask_phone(issue.phone) if issue.phone else None,
        "otp": {"expires_in": issue.expires_in, "resend_in": issue.resend_in, "length": issue.length},
        "social_ticket": None,
        "provider": None,
        "email_mask": mask_email(issue.email) if issue.email else None,
        # The step lasts as long as the app may keep the password and the verification token (C§2.0,
        # §2.1.4: 10 min), not the code's own TTL (that is otp.expires_in).
        "expires_in": int(settings.CUSTOMER_VERIFICATION_TTL_SECONDS),
    }


def phone_step_body(step: services.PhoneStep) -> dict[str, Any]:
    return {
        "next_step": enums.NextStep.VERIFY_PHONE.value,
        "phone_mask": None,
        "otp": None,
        "social_ticket": step.ticket,
        "provider": step.provider,
        "email_mask": mask_email(step.email) if step.email else None,
        "expires_in": step.expires_in,
    }


def otp_invalid_response(exc: OtpInvalid, request: Request, lang: str) -> Response:
    """400 ``otp_invalid`` with ``details.attempts_left`` and the field error on ``code`` (customer contract
    §2.1.2; also ``me/phone/`` and ``me/email/``)."""
    left = int(exc.details["attempts_left"])
    body = customer_error_body(
        "otp_invalid",
        request,
        lang=lang,
        params={"n": left},
        details=exc.details,
        errors=[
            {"field": "code", "code": "otp_invalid", "message": i18n.t("field.otp_invalid", lang, n=left)}
        ],
    )
    return Response(body, status=400)


def require_social_available(provider: object = None) -> None:
    """404 while customer social sign-in is off (fake OAuth under the production settings), and for an
    unknown or unavailable provider (customer contract §2.1.8)."""
    if integration_modes.fake_in_production("oauth"):
        raise NotFound
    if provider is not None and (
        provider not in CUSTOMER_PROVIDERS or not customer_provider_available(str(provider))
    ):
        raise NotFound


class SendCodeView(CustomerAuthAPIView):
    throttle_classes = [SmsPhoneThrottle, SmsDeviceThrottle, SmsIPThrottle]

    @extend_schema(
        operation_id="customer_auth_send_code",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.SendCodeSerializer,
        responses={200: s.CodeIssueSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.SendCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = device_context(self, request)
        jti = None
        if data["purpose"] == enums.OtpPurpose.LINK_SOCIAL:
            require_social_available()
            jti = services.read_social_ticket(str(data["social_ticket"]), fid=device.fid).jti
        issue = account_services.send_code(
            data["identifier"],
            data["purpose"],
            ip=device.ip,
            fid=device.fid,
            lang=device.lang,
            platform=device.platform,
            social_jti=jti,
        )
        return Response(code_issue_body(issue))


class VerifyCodeView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_verify_code",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.VerifyCodeSerializer,
        responses={200: s.VerificationSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = account_services.verify_code(data["identifier"], data["purpose"], data["code"])
        except OtpInvalid as exc:
            return otp_invalid_response(exc, request, self.language)
        return Response(
            {
                "verification_token": result.token,
                "expires_in": result.expires_in,
                "purpose": result.purpose,
                "customer_exists": result.customer_exists,
            }
        )


class SetPasswordView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_set_password",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.SetPasswordSerializer,
        responses={201: s.AuthTokensSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        if not site_config_selectors.current_terms().published:
            raise TermsNotPublished()
        account_services.verification_row(
            str(request.data.get("verification_token") or "") if isinstance(request.data, dict) else "",
            purpose=enums.OtpPurpose.REGISTER,
        )
        serializer = s.SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = device_context(self, request)
        sign_in = services.register(
            data["verification_token"],
            password=data["password"],
            name=data["name"],
            terms_version=data["terms_version"],
            language=data.get("language") or device.lang,
            device=device,
            request=request,
        )
        return Response(auth_tokens(sign_in, fid=device.fid, request=request), status=201)


class LoginView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_login",
        auth=[],
        parameters=[*AUTH_PARAMETERS, DEVICE_KEY_HEADER],
        request=s.CustomerLoginSerializer,
        responses={200: AUTH_TOKENS_OR_NEXT_STEP, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.CustomerLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = device_context(self, request)
        result = services.login(
            data["identifier"],
            data["password"],
            data.get("verification_token") or None,
            device=device,
            device_key=str(request.headers.get("X-GD-Device-Key", "")) or None,
            request=request,
        )
        if isinstance(result, services.DeviceStep):
            return Response(device_step_body(result))
        return Response(auth_tokens(result, fid=device.fid, request=request))


class ResetPasswordView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_reset_password",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.ResetPasswordSerializer,
        responses={200: s.AuthTokensSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = device_context(self, request)
        sign_in = services.reset_password(
            data["verification_token"], password=data["password"], device=device, request=request
        )
        return Response(auth_tokens(sign_in, fid=device.fid, request=request))


class RefreshView(CustomerAuthAPIView):
    throttle_classes = [AuthClientIPThrottle, RefreshRateThrottle, RefreshUnknownThrottle]

    @extend_schema(
        operation_id="customer_auth_refresh",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.CustomerRefreshSerializer,
        responses={200: s.RefreshResponseSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.CustomerRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = cast(DeviceHeaders, self.device)
        try:
            result = account_services.rotate_customer_refresh(
                serializer.validated_data["refresh"],
                fid=device.fid,
                platform=device.platform,
                request=request,
            )
        except account_services.RefreshUnknown:
            RefreshUnknownThrottle().record_miss(request, self)
            raise
        return Response(result.body)


class LogoutView(CustomerAuthAPIView):
    throttle_classes = [AuthClientIPThrottle]

    @extend_schema(
        operation_id="customer_auth_logout",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.CustomerLogoutSerializer,
        responses={204: None, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.CustomerLogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account_services.logout_customer(
            serializer.validated_data["refresh"], scope=serializer.validated_data["scope"], request=request
        )
        return Response(status=204)


class SocialNonceView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_social_nonce",
        auth=[],
        parameters=AUTH_PARAMETERS,
        responses={200: s.NonceSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        require_social_available()
        nonce, ttl = account_services.issue_social_nonce(cast(DeviceHeaders, self.device).fid)
        return Response({"nonce": nonce, "expires_in": ttl})


class SocialView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_social",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.SocialSerializer,
        responses={200: AUTH_TOKENS_OR_NEXT_STEP, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        require_social_available(request.data.get("provider") if isinstance(request.data, dict) else None)
        serializer = s.SocialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = device_context(self, request)
        result = services.social_sign_in(
            data["provider"],
            data["token"],
            data.get("token_type"),
            data.get("nonce"),
            data.get("authorization_code"),
            device=device,
            request=request,
        )
        if isinstance(result, services.PhoneStep):
            return Response(phone_step_body(result))
        return Response(auth_tokens(result, fid=device.fid, request=request))


class SocialCompleteView(CustomerAuthAPIView):
    @extend_schema(
        operation_id="customer_auth_social_complete",
        auth=[],
        parameters=AUTH_PARAMETERS,
        request=s.SocialCompleteSerializer,
        responses={200: s.AuthTokensSerializer, 201: s.AuthTokensSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        require_social_available()
        serializer = s.SocialCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = device_context(self, request)
        sign_in = services.complete_social(
            data["social_ticket"],
            data.get("verification_token") or None,
            password=data.get("password"),
            name=data.get("name"),
            terms_version=data.get("terms_version"),
            language=data.get("language"),
            device=device,
            request=request,
        )
        status = 201 if sign_in.is_new_customer or sign_in.reactivated else 200
        return Response(auth_tokens(sign_in, fid=device.fid, request=request), status=status)
