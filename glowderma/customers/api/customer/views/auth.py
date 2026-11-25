"""``auth/*`` of the customer API (customer contract §2.1): OTP, registration, login with the new-device code,
reset, rotating refresh and logout."""

from __future__ import annotations

from typing import cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import (
    CustomerAuthAPIView,
)
from glowderma.accounts.api.throttles import (
    AuthClientIPThrottle,
    RefreshRateThrottle,
    RefreshUnknownThrottle,
    SmsDeviceThrottle,
    SmsIPThrottle,
    SmsPhoneThrottle,
)
from glowderma.accounts.authentication import DeviceHeaders
from glowderma.core import enums
from glowderma.core.api.exceptions import OtpInvalid, TermsNotPublished
from glowderma.customers import services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.payloads import code_issue_body, device_step_body
from glowderma.customers.api.customer.schemas.auth import (
    LOGIN_POST,
    LOGOUT_POST,
    REFRESH_POST,
    RESET_PASSWORD_POST,
    SEND_CODE_POST,
    SET_PASSWORD_POST,
    VERIFY_CODE_POST,
)
from glowderma.customers.api.customer.views.common import (
    auth_tokens,
    device_context,
    otp_invalid_response,
    require_social_available,
)
from glowderma.site_config import selectors as site_config_selectors


class SendCodeView(CustomerAuthAPIView):
    throttle_classes = [SmsPhoneThrottle, SmsDeviceThrottle, SmsIPThrottle]

    @SEND_CODE_POST
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
    @VERIFY_CODE_POST
    def post(self, request: Request) -> Response:
        serializer = s.VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        target = data["identifier"]
        if data.get("otp_channel"):  # the new-device code went to the account's other identifier
            target = services.code_target(data["identifier"], data["otp_channel"]) or target
        try:
            result = account_services.verify_code(target, data["purpose"], data["code"])
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
    @SET_PASSWORD_POST
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
    @LOGIN_POST
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
            otp_channel=data.get("otp_channel") or None,
        )
        if isinstance(result, services.DeviceStep):
            return Response(device_step_body(result))
        return Response(auth_tokens(result, fid=device.fid, request=request))


class ResetPasswordView(CustomerAuthAPIView):
    @RESET_PASSWORD_POST
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

    @REFRESH_POST
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

    @LOGOUT_POST
    def post(self, request: Request) -> Response:
        serializer = s.CustomerLogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account_services.logout_customer(
            serializer.validated_data["refresh"], scope=serializer.validated_data["scope"], request=request
        )
        return Response(status=204)


__all__ = [
    "LoginView",
    "LogoutView",
    "RefreshView",
    "ResetPasswordView",
    "SendCodeView",
    "SetPasswordView",
    "VerifyCodeView",
]
