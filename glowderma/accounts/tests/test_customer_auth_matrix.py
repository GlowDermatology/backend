"""The URL-walk auth matrix of the customer audience (customer contract §6.2, plan §1 P0).

Every route under ``api/v1/`` that is not admin, health or schema is classified from its view class — **C**
(``CustomerAPIView`` / ``CustomerGenericViewSet``), **O** (``PublicAPIView``), **S** (``WebhookAPIView``),
**K** (a ``pos`` audience view) or ``auth`` (``CustomerAuthAPIView``) — and probed accordingly. A route under
the customer prefixes that fits none of them fails the test, so a package cannot mount an unguarded view.
"""

from base64 import b64encode
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import override_settings
from django.urls import URLPattern, URLResolver, clear_url_caches, get_resolver, path
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from glowderma.accounts import services, tokens
from glowderma.accounts.api.customer_base import (
    CustomerAPIView,
    CustomerAuthAPIView,
    CustomerGenericViewSet,
    PublicAPIView,
    WebhookAPIView,
)
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, device_headers, start_test_session
from glowderma.accounts.tests.test_admin_auth_matrix import route_kwarg
from glowderma.core.api.parsers import JSONParser

EXCLUDED_PREFIXES = ("api/v1/admin/", "api/v1/health/", "api/v1/schema/", "api/v1/docs/")


def classify(view_class: type | None) -> str:
    if view_class is None:
        return "unknown"
    if issubclass(view_class, (CustomerAPIView, CustomerGenericViewSet)):
        return "C"
    if issubclass(view_class, PublicAPIView):
        return "O"
    if issubclass(view_class, WebhookAPIView):
        return "S"
    if issubclass(view_class, CustomerAuthAPIView):
        return "auth"
    if getattr(view_class, "audience", None) == "pos":
        return "K"
    return "unknown"


def customer_routes(
    patterns: list[Any], prefix: str = "", values: dict[str, str] | None = None
) -> Iterator[tuple[str, str, str]]:
    """``(path, method, class)`` for every customer-audience route (sample values fill the URL kwargs)."""
    for pattern in patterns:
        raw = prefix + str(pattern.pattern)
        kwargs = dict(values or {})
        converters = getattr(pattern.pattern, "converters", {})
        for name in set(converters) | set(pattern.pattern.regex.groupindex):
            converter = converters.get(name)
            kwargs[name] = route_kwarg(name, type(converter).__name__ if converter is not None else None)
        if isinstance(pattern, URLResolver):
            yield from customer_routes(pattern.url_patterns, raw, kwargs)
            continue
        route = raw.replace("^", "").replace("$", "")
        if not isinstance(pattern, URLPattern) or not route.startswith("api/v1/"):
            continue
        if route.startswith(EXCLUDED_PREFIXES):
            continue
        concrete = "/" + route
        for name, value in kwargs.items():
            concrete = concrete.replace(f"<{name}>", value)
            for converter in ("int", "str", "slug", "uuid", "path"):
                concrete = concrete.replace(f"<{converter}:{name}>", value)
        view_class = getattr(pattern.callback, "cls", None) or getattr(pattern.callback, "view_class", None)
        actions = getattr(pattern.callback, "actions", {})
        if view_class is None:
            methods = {"get", "post"}
        elif actions:
            methods = set(actions)
        else:
            methods = {method for method in view_class.http_method_names if hasattr(view_class, method)}
        kind = classify(view_class)
        for method in sorted(methods - {"options", "head"}):
            yield concrete, method, kind


def probe(client: APIClient, method: str, route: str, **extra: Any) -> int:
    return int(getattr(client, method)(route, {}, format="json", **extra).status_code)


def run_matrix(routes: list[tuple[str, str, str]]) -> None:
    staff = StaffUser.objects.create_user("matrix@x.uz", "Saffron!Harbor-Long72")
    staff_session, _ = services.start_staff_session(staff, device_label="matrix")
    staff_token, _ = tokens.issue_access(staff_session)
    customer = CustomerFactory.create()
    customer_session = start_test_session(customer).session
    customer_token, _ = tokens.issue_access(customer_session)
    revoked_customer = CustomerFactory.create()
    revoked_session = start_test_session(revoked_customer, fid="fid-revoked-01").session
    revoked_token, _ = tokens.issue_access(revoked_session)
    services.revoke_customer_sessions(revoked_customer.pk, "logout")
    basic = "Basic " + b64encode(b"matrix@x.uz:Saffron!Harbor-Long72").decode()
    headers = device_headers()
    for route, method, kind in routes:
        assert kind != "unknown", f"{route} is mounted under the customer API without a customer base view"
        if kind == "C":
            for authorization in (None, f"Bearer {staff_token}", basic, f"Bearer {revoked_token}"):
                client = APIClient()
                if authorization:
                    client.credentials(HTTP_AUTHORIZATION=authorization)
                assert probe(client, method, route, **headers) == 401, (route, method, authorization)
            session_client = APIClient()
            session_client.force_login(staff)
            assert probe(session_client, method, route, **headers) == 401, (route, method, "session")
        elif kind == "O":
            for authorization in (f"Bearer {staff_token}", basic, "Bearer junk", f"Bearer {revoked_token}"):
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=authorization)
                assert probe(client, method, route, **headers) == 401, (route, method, authorization)
            assert probe(APIClient(), method, route) not in (401, 403), (route, method, "anonymous")
        elif kind == "K":
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {customer_token}")
            assert probe(client, method, route) == 401, (route, method, "customer token on POS")
        elif kind == "S":
            plain = probe(APIClient(), method, route)
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {customer_token}")
            assert probe(client, method, route) == plain, (route, method, "a token must change nothing")
        elif kind == "auth":
            assert probe(APIClient(), method, route) == 400, (route, method, "device headers required")


@pytest.mark.django_db
def test_customer_auth_matrix_over_the_mounted_routes() -> None:
    routes = list(customer_routes(get_resolver().url_patterns))
    run_matrix(routes)


# --- the walker itself, over sample views of every class
# ------------------------------------------------------


class SampleCustomerView(CustomerAPIView):
    def get(self, request: Request, pk: int) -> Response:
        return Response({})

    def post(self, request: Request, pk: int) -> Response:
        return Response({})


class SampleCustomerViewSet(CustomerGenericViewSet):
    def list(self, request: Request) -> Response:
        return Response({})


class SamplePublicView(PublicAPIView):
    def get(self, request: Request) -> Response:
        return Response({})


class SampleAuthView(CustomerAuthAPIView):
    def post(self, request: Request) -> Response:
        return Response({})


class SampleHookView(WebhookAPIView):
    parser_classes = [JSONParser]

    def post(self, request: Request) -> Response:
        return Response({"ok": True})


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> None:
        return None

    def authenticate_header(self, request: Request) -> str:
        return "Api-Key"


class IsPosDevice(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        return False


class SamplePosView(APIView):
    audience = "pos"
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsPosDevice]

    def post(self, request: Request) -> Response:  # pragma: no cover - always refused
        return Response({})


def plain_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse("unguarded")


sample_urlpatterns = [
    path("api/v1/me/things/<int:pk>/", SampleCustomerView.as_view()),
    path("api/v1/cart/", SampleCustomerViewSet.as_view({"get": "list"})),
    path("api/v1/catalog/sample/", SamplePublicView.as_view()),
    path("api/v1/auth/sample/", SampleAuthView.as_view()),
    path("api/v1/webhooks/sample/", SampleHookView.as_view()),
    path("api/v1/pos/sample/", SamplePosView.as_view()),
    path("api/v1/admin/ignored/", plain_view),
]


def walk(urlpatterns: list[Any]) -> list[tuple[str, str, str]]:
    module: Any = ModuleType("customer_matrix_urls")
    module.urlpatterns = urlpatterns
    try:
        with override_settings(ROOT_URLCONF=module):
            clear_url_caches()
            return list(customer_routes(get_resolver().url_patterns))
    finally:
        clear_url_caches()


def test_the_walker_classifies_every_customer_base() -> None:
    assert sorted(walk(sample_urlpatterns)) == [
        ("/api/v1/auth/sample/", "post", "auth"),
        ("/api/v1/cart/", "get", "C"),
        ("/api/v1/catalog/sample/", "get", "O"),
        ("/api/v1/me/things/1/", "get", "C"),
        ("/api/v1/me/things/1/", "post", "C"),
        ("/api/v1/pos/sample/", "post", "K"),
        ("/api/v1/webhooks/sample/", "post", "S"),
    ]


@pytest.mark.django_db
def test_the_matrix_passes_over_the_sample_views() -> None:
    module: Any = ModuleType("customer_matrix_urls_run")
    module.urlpatterns = sample_urlpatterns
    try:
        with override_settings(ROOT_URLCONF=module):
            clear_url_caches()
            run_matrix(list(customer_routes(get_resolver().url_patterns)))
    finally:
        clear_url_caches()


def test_an_unguarded_customer_route_fails_the_matrix() -> None:
    routes = walk([path("api/v1/me/leak/", plain_view)])
    assert routes == [("/api/v1/me/leak/", "get", "unknown"), ("/api/v1/me/leak/", "post", "unknown")]
    with pytest.raises(AssertionError, match="without a customer base view"):
        for _route, _method, kind in routes:
            assert kind != "unknown", "mounted under the customer API without a customer base view"
