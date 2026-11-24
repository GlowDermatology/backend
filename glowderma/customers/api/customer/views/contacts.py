"""``me/phone/*`` and ``me/email/*`` of the customer API (customer contract §2.2): attaching a verified phone
or e-mail."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle

from glowderma.accounts.api.throttles import (
    CustomerRateThrottle,
    SmsDeviceThrottle,
    SmsPhoneThrottle,
)
from glowderma.core.api.exceptions import OtpInvalid
from glowderma.customers import services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.payloads import code_issue_body
from glowderma.customers.api.customer.schemas.contacts import (
    EMAIL_CODE_POST,
    EMAIL_POST,
    PHONE_CODE_POST,
    PHONE_POST,
)
from glowderma.customers.api.customer.views.common import MeBase, device_context, otp_invalid_response

CODE_THROTTLES: list[type[BaseThrottle]] = [CustomerRateThrottle, SmsPhoneThrottle, SmsDeviceThrottle]


class PhoneCodeView(MeBase):
    throttle_classes = CODE_THROTTLES

    @PHONE_CODE_POST
    def post(self, request: Request) -> Response:
        serializer = s.MePhoneCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        issue = services.send_attach_phone_code(
            self.customer.pk, serializer.validated_data["phone"], device=device_context(self, request)
        )
        return Response(code_issue_body(issue))


class PhoneView(MeBase):
    throttle_classes = CODE_THROTTLES  # the code check is per target and device, like send-code

    @PHONE_POST
    def post(self, request: Request) -> Response:
        serializer = s.MePhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            services.attach_phone(
                self.customer.pk,
                data["phone"],
                data["code"],
                password=data.get("password"),
                verification_token=data.get("verification_token"),
                sid=self.sid,
                request=request,
            )
        except OtpInvalid as exc:
            return otp_invalid_response(exc, request, self.language)
        return Response(self.me())


class EmailCodeView(MeBase):
    throttle_classes = CODE_THROTTLES

    @EMAIL_CODE_POST
    def post(self, request: Request) -> Response:
        serializer = s.MeEmailCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        issue = services.send_verify_email_code(
            self.customer.pk, serializer.validated_data["email"], device=device_context(self, request)
        )
        return Response(code_issue_body(issue))


class EmailView(MeBase):
    throttle_classes = CODE_THROTTLES  # the code check is per target and device, like send-code

    @EMAIL_POST
    def post(self, request: Request) -> Response:
        serializer = s.MeEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            services.attach_email(
                self.customer.pk,
                data["email"],
                data["code"],
                password=data.get("password"),
                verification_token=data.get("verification_token"),
                sid=self.sid,
                request=request,
            )
        except OtpInvalid as exc:
            return otp_invalid_response(exc, request, self.language)
        return Response(self.me())


__all__ = [
    "CODE_THROTTLES",
    "EmailCodeView",
    "EmailView",
    "PhoneCodeView",
    "PhoneView",
]
