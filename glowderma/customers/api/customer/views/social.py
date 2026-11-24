"""``auth/social/*`` of the customer API (customer contract §2.1.8): social sign-in with Google, Facebook and
Apple, and its phone completion."""

from __future__ import annotations

from typing import cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import (
    CustomerAuthAPIView,
)
from glowderma.accounts.authentication import DeviceHeaders
from glowderma.customers import services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.payloads import phone_step_body
from glowderma.customers.api.customer.schemas.social import (
    SOCIAL_COMPLETE_POST,
    SOCIAL_NONCE_GET,
    SOCIAL_POST,
)
from glowderma.customers.api.customer.views.common import (
    auth_tokens,
    device_context,
    require_social_available,
)


class SocialNonceView(CustomerAuthAPIView):
    @SOCIAL_NONCE_GET
    def get(self, request: Request) -> Response:
        require_social_available()
        nonce, ttl = account_services.issue_social_nonce(cast(DeviceHeaders, self.device).fid)
        return Response({"nonce": nonce, "expires_in": ttl})


class SocialView(CustomerAuthAPIView):
    @SOCIAL_POST
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
    @SOCIAL_COMPLETE_POST
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


__all__ = [
    "SocialCompleteView",
    "SocialNonceView",
    "SocialView",
]
