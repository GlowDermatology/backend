"""What several customer views share: the client facts, the device context of a sign-in, the token answer, the
``otp_invalid`` answer, the social availability gate and the ``me/`` base view."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import request_facts as account_request_facts
from glowderma.accounts import services as account_services
from glowderma.accounts import tokens
from glowderma.accounts.api.customer_base import (
    CustomerAPIView,
)
from glowderma.accounts.authentication import DeviceHeaders
from glowderma.accounts.models import Customer
from glowderma.core import i18n, integration_modes, timeutils
from glowderma.core.api.exceptions import OtpInvalid, customer_error_body
from glowderma.customers import selectors, services
from glowderma.integrations.oauth.registry import CUSTOMER_PROVIDERS, customer_provider_available


def customer_id(request: Request) -> int | None:
    """The signed-in customer's id on an optional-auth (**O**) route, else ``None``."""
    user: object = request.user
    return user.pk if isinstance(user, Customer) else None


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
    user_agent, ip = account_request_facts.activity_facts(request, platform=platform)
    account_services.stamp_session(session.pk, user_agent=user_agent, ip=ip)
    access, expires = tokens.issue_access(session)
    customer = Customer.objects.get(pk=sign_in.customer_id)
    return {
        "access": access,
        "access_expires_at": timeutils.isoformat(expires),
        "refresh": sign_in.started.raw_refresh,
        "refresh_expires_at": timeutils.isoformat(session.absolute_expires_at),
        "device_key": sign_in.started.raw_device_key,
        "customer": selectors.me_payload(customer, fid=fid),
        "is_new_customer": sign_in.is_new_customer,
        "reactivated": sign_in.reactivated,
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


class MeBase(CustomerAPIView):
    @property
    def customer(self) -> Customer:
        return cast(Customer, self.request.user)

    @property
    def fid(self) -> str:
        return cast(DeviceHeaders, self.device).fid

    @property
    def sid(self) -> str:
        """The request's session id: every write re-checks it under the customer-level lock."""
        return str(cast(dict[str, Any], self.request.auth)["sid"])

    def me(self) -> dict[str, Any]:
        return selectors.me_payload(Customer.objects.get(pk=self.customer.pk), fid=self.fid)


__all__ = [
    "MeBase",
    "auth_tokens",
    "client_ip",
    "customer_id",
    "device_context",
    "otp_invalid_response",
    "require_social_available",
]
