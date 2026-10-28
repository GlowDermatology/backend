"""Base views of the customer audience (customer contract §1.1–§1.3; plan §1 P0).

Base classes (``audience`` drives the exception handler):

* ``CustomerAPIView`` (C, audience ``customer``): ``CustomerJWTAuthentication``, ``IsActiveCustomer``,
  throttle ``customer``.
* ``CustomerAuthAPIView`` (``auth/*``, audience ``customer_auth``): no authentication (logout adds one),
  ``AllowAny``, throttles ``customer_auth_device`` and ``customer_auth_ip``.
* ``PublicAPIView`` (O, audience ``public``): ``OptionalCustomerAuthentication``, ``AllowAny``, throttles
  ``public`` and ``public_ip`` (anonymous) or ``customer`` (signed in).
* ``WebhookAPIView`` (S, audience ``webhook``): no authentication (provider signatures), ``AllowAny``, no
  throttle.

``PosAPIView`` (K) lives in ``loyalty/api/pos/base.py`` (P3): its authentication reads ``loyalty.PosDevice``,
which ``accounts`` may not import. Every class except the webhook one parses the device headers first
(:class:`DeviceHeadersMixin`: 400 ``device_required``, 426 ``app_update_required``), and every response gets
``Content-Language`` and ``Cache-Control: no-store`` unless the view declares a public cache window
(``public_cache_seconds``, O routes only). The ``audience`` attribute switches the exception handler to the
customer envelope (``details``, messages from ``core.i18n``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from glowderma.accounts.api.throttles import (
    CustomerAuthDeviceThrottle,
    CustomerAuthIPThrottle,
    CustomerRateThrottle,
    PublicBffThrottle,
    PublicDeviceThrottle,
    PublicIPThrottle,
)
from glowderma.accounts.authentication import (
    REQUEST_DEVICE_ATTR,
    CustomerJWTAuthentication,
    DeviceHeaders,
    OptionalCustomerAuthentication,
    parse_device_headers,
)
from glowderma.accounts.permissions import IsActiveCustomer
from glowderma.core.api.exceptions import request_language
from glowderma.core.api.parsers import JSONParser


class DeviceHeadersMixin:
    """Parses the device headers before authentication and finishes every response (language, caching)."""

    audience: ClassVar[str] = "customer"
    device_headers_required: ClassVar[bool] = True
    # O routes only: seconds an anonymous GET 200 may be cached publicly (customer contract §1.3); None =
    # no-store.
    public_cache_seconds: ClassVar[int | None] = None

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        device = parse_device_headers(request, required=self.device_headers_required)
        setattr(request._request, REQUEST_DEVICE_ATTR, device)
        super().initial(request, *args, **kwargs)  # type: ignore[misc]

    @property
    def device(self) -> DeviceHeaders | None:
        request: Request = self.request  # type: ignore[attr-defined]
        device = getattr(request._request, REQUEST_DEVICE_ATTR, None)
        return device if isinstance(device, DeviceHeaders) else None

    @property
    def language(self) -> str:
        """The resolved response language (Accept-Language → the customer's language → uz)."""
        return request_language(self.request)  # type: ignore[attr-defined]

    def finalize_response(self, request: Request, response: Response, *args: Any, **kwargs: Any) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)  # type: ignore[misc]
        if not response.has_header("Content-Language"):
            response["Content-Language"] = request_language(request)
        if not response.has_header("Cache-Control"):
            seconds = self.public_cache_seconds
            if seconds is not None and request.method == "GET" and response.status_code == 200:
                if request.headers.get("Authorization"):
                    response["Cache-Control"] = "private, no-cache"
                    response["Vary"] = "Accept-Language, Authorization"
                else:
                    response["Cache-Control"] = f"public, max-age={seconds}"
                    response["Vary"] = "Accept-Language"
            else:
                response["Cache-Control"] = "no-store"
        return response


class CustomerAPIView(DeviceHeadersMixin, APIView):
    """Signed-in customer routes (**C**)."""

    audience = "customer"
    parser_classes = [JSONParser]
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsActiveCustomer]
    throttle_classes = [CustomerRateThrottle]


class CustomerGenericViewSet(DeviceHeadersMixin, GenericViewSet[Any]):
    """Signed-in customer routes (**C**) served by a viewset."""

    audience = "customer"
    parser_classes = [JSONParser]
    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsActiveCustomer]
    throttle_classes = [CustomerRateThrottle]


class CustomerAuthAPIView(DeviceHeadersMixin, APIView):
    """``auth/*`` routes (before sign-in): device headers required, no bearer (``auth/logout/`` adds the
    optional one), throttles ``customer_auth_device`` + ``customer_auth_ip`` unless the view names its own."""

    audience = "customer_auth"
    parser_classes = [JSONParser]
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [
        CustomerAuthDeviceThrottle,
        CustomerAuthIPThrottle,
    ]


class PublicAPIView(DeviceHeadersMixin, APIView):
    """Optional-auth routes (**O**): anonymous callers count against ``public`` / ``public_ip``, signed-in
    callers against ``customer``; device headers optional."""

    audience = "public"
    device_headers_required = False
    parser_classes = [JSONParser]
    authentication_classes = [OptionalCustomerAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [
        PublicDeviceThrottle,
        PublicIPThrottle,
        PublicBffThrottle,
        CustomerRateThrottle,
    ]


class WebhookAPIView(APIView):
    """Provider callbacks (**S**, customer contract §2.13): no DRF authentication (signatures are checked by
    the view), no throttle, parsers declared per provider view, provider-native bodies. DRF's ``as_view``
    already exempts API views from CSRF; there is no cookie or session auth on this API.

    Errors the view does not answer itself (405, 415, a parse error, an unexpected exception) use the customer
    envelope (``audience`` ``webhook`` is in ``CUSTOMER_AUDIENCES``). A provider whose protocol needs a native
    error body (Click, Payme: P2) overrides ``get_exception_handler`` on its view."""

    audience: ClassVar[str] = "webhook"
    parser_classes = []
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def finalize_response(self, request: Request, response: Response, *args: Any, **kwargs: Any) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)
        if not response.has_header("Cache-Control"):
            response["Cache-Control"] = "no-store"
        return response
