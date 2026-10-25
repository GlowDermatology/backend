from base64 import b64encode
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest
from django.http import HttpRequest, HttpResponse
from django.test import override_settings
from django.urls import (
    NoReverseMatch,
    URLPattern,
    URLResolver,
    clear_url_caches,
    get_resolver,
    path,
    re_path,
    resolve,
    reverse,
)
from django.views import View
from rest_framework.test import APIClient

from glowderma.accounts import services, tokens
from glowderma.accounts.models import StaffUser

# Sample values by URL kwarg name; any ``*_id``, ``pk``, ``id`` and ``version`` resolve to "1".
KWARG_VALUES = {
    "format": "json",
    "slug": "serums",
    "status": "processing",
    "provider": "google",
    "action": "confirm",
    "date": "2026-10-26",
}
# The only (route, method) pairs an anonymous caller may use (contract 1.2 and 2.1); everything else under
# api/v1/admin/ must reject every non-staff credential. ``auth/oauth/<provider>/`` is public for POST only:
# DELETE on the same route (unlink) and ``.../link/`` need a bearer.
PUBLIC_METHODS = {
    (f"/api/v1/admin/auth/{name}/", "post") for name in ("login", "refresh", "logout", "oauth/google")
}
# Views without any authentication class answer other methods with 405 (OPTIONS 200) instead of 401.
UNAUTHENTICATED_VIEWS = {f"/api/v1/admin/auth/{name}/" for name in ("login", "refresh", "logout")}
CONVERTER_VALUES = {
    "UUIDConverter": "00000000-0000-4000-8000-000000000001",
    "IntConverter": "1",
    "SlugConverter": "serums",
    "StringConverter": "example",
    "PathConverter": "example",
}


def route_kwarg(name: str, converter: str | None = None) -> str:
    """A sample value that satisfies a URL kwarg by its converter class name or, failing that, its name."""
    if converter in ("UUIDConverter", "IntConverter"):
        return CONVERTER_VALUES[converter]
    if name in KWARG_VALUES:
        return KWARG_VALUES[name]
    if name in {"pk", "id", "version"} or name.endswith("_id"):
        return "1"
    return CONVERTER_VALUES.get(converter or "", "example")


def protected_routes(
    patterns: list[Any],
    prefix: str = "",
    namespace: tuple[str, ...] = (),
    values: dict[str, Any] | None = None,
) -> Iterator[tuple[str, str]]:
    for pattern in patterns:
        raw = prefix + str(pattern.pattern)
        kwargs = dict(values or {})
        converters = getattr(pattern.pattern, "converters", {})
        names = set(converters) | set(pattern.pattern.regex.groupindex)
        for name in names:
            converter = converters.get(name)
            kwargs[name] = route_kwarg(name, type(converter).__name__ if converter is not None else None)
        if isinstance(pattern, URLResolver):
            nested = namespace + ((pattern.namespace,) if pattern.namespace else ())
            yield from protected_routes(pattern.url_patterns, raw, nested, kwargs)
        elif isinstance(pattern, URLPattern) and raw.replace("^", "").replace("$", "").startswith(
            "api/v1/admin/"
        ):
            target = ":".join((*namespace, pattern.name)) if pattern.name else pattern.callback
            try:
                path_value = reverse(target, kwargs=kwargs)
            except NoReverseMatch as exc:
                pytest.fail(f"Cannot reverse admin route {raw}: {exc}")
            assert path_value.startswith("/api/v1/admin/"), path_value
            if resolve(path_value).route.replace("^", "").replace("$", "") != raw.replace("^", "").replace(
                "$", ""
            ):
                pytest.fail(f"Cannot reverse admin route uniquely: {raw}; give the route a unique name")
            view_class = getattr(pattern.callback, "cls", None) or getattr(
                pattern.callback, "view_class", None
            )
            actions = getattr(pattern.callback, "actions", {})
            if view_class is None:
                methods = {"get", "post", "put", "patch", "delete"}
            elif actions:
                methods = set(actions)
            else:
                methods = {method for method in view_class.http_method_names if hasattr(view_class, method)}
            for method in sorted(methods | {"head", "options"}):
                yield path_value, method


@pytest.mark.django_db
def test_admin_auth_matrix(django_capture_on_commit_callbacks: Any) -> None:
    routes = list(protected_routes(get_resolver().url_patterns))
    assert routes
    user = StaffUser.objects.create_user("admin@x.uz", "Saffron!Harbor-Long72")
    session, _ = services.start_staff_session(user, device_label="test")
    session.audience = "customer"
    session.customer_id = user.pk
    customer_token, _ = tokens.issue_access(session)
    session.audience = "staff"
    revoked_token, _ = tokens.issue_access(session)
    with django_capture_on_commit_callbacks(execute=True):
        services.revoke_session(session, "logout")
    basic = b64encode(b"admin@x.uz:Saffron!Harbor-Long72").decode()
    for route_path, method in routes:
        if (route_path, method) in PUBLIC_METHODS:
            continue
        expected = (200 if method == "options" else 405) if route_path in UNAUTHENTICATED_VIEWS else 401
        for header in (None, f"Basic {basic}", f"Bearer {customer_token}", f"Bearer {revoked_token}"):
            client = APIClient()
            if header:
                client.credentials(HTTP_AUTHORIZATION=header)
            assert getattr(client, method)(route_path, {}, format="json").status_code == expected, (
                route_path,
                method,
            )
        session_client = APIClient()
        session_client.force_login(user)
        assert getattr(session_client, method)(route_path, {}, format="json").status_code == expected, (
            route_path,
            method,
        )


def plain_view(request: HttpRequest, **kwargs: Any) -> HttpResponse:
    return HttpResponse("unguarded")


class PlainView(View):
    def get(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        return HttpResponse("unguarded")


urlpatterns = [
    path("api/v1/admin/function/", plain_view, name="matrix-function"),
    path("api/v1/admin/class/", PlainView.as_view(), name="matrix-class"),
    re_path(r"^api/v1/admin/regex/(?P<pk>[0-9]+)/$", plain_view, name="matrix-regex"),
]


@override_settings(ROOT_URLCONF=__name__)
def test_matrix_includes_django_views_regex_head_and_options() -> None:
    routes = set(protected_routes(get_resolver().url_patterns))
    for path_value in ("/api/v1/admin/function/", "/api/v1/admin/class/", "/api/v1/admin/regex/1/"):
        assert {(path_value, method) for method in ("get", "head", "options")} <= routes


def test_matrix_fails_when_a_route_cannot_be_reversed() -> None:
    module: Any = ModuleType("unbuildable_matrix_urls")
    module.urlpatterns = [re_path(r"^api/v1/admin/code/(?P<code>[A-Z]{3})/$", plain_view, name="matrix-code")]
    try:
        with override_settings(ROOT_URLCONF=module):
            clear_url_caches()
            with pytest.raises(pytest.fail.Exception, match="Cannot reverse"):
                list(protected_routes(get_resolver().url_patterns))
    finally:
        clear_url_caches()


@pytest.mark.parametrize(
    "name,converter,expected",
    [
        ("sid", "UUIDConverter", "00000000-0000-4000-8000-000000000001"),
        ("slug", "IntConverter", "1"),
        ("slug", "SlugConverter", "serums"),
        ("slug", "StringConverter", "serums"),
        ("status", "StringConverter", "processing"),
        ("provider", "StringConverter", "google"),
        ("action", "StringConverter", "confirm"),
        ("date", "StringConverter", "2026-10-26"),
        ("format", None, "json"),
        ("pk", None, "1"),
        ("id", "StringConverter", "1"),
        ("version", None, "1"),
        ("order_id", "StringConverter", "1"),
        ("customer_id", None, "1"),
        ("code", "StringConverter", "example"),
        ("anything", None, "example"),
        ("anything", "PathConverter", "example"),
    ],
)
def test_route_kwarg_filler(name: str, converter: str | None, expected: str) -> None:
    """Converter class wins for UUID/int, then the kwarg name, then ``*_id``/pk/id/version, then "example"."""
    assert route_kwarg(name, converter) == expected


m1_urlpatterns = [
    path("api/v1/admin/categories/<slug:slug>/", plain_view, name="matrix-m1-category"),
    path("api/v1/admin/orders/<int:order_id>/transition/<str:status>/", plain_view, name="matrix-m1-order"),
    path("api/v1/admin/auth/oauth/<str:provider>/", plain_view, name="matrix-m1-oauth"),
    path("api/v1/admin/appointments/<int:pk>/<str:action>/", plain_view, name="matrix-m1-appointment"),
    path("api/v1/admin/dashboard/<str:date>/", plain_view, name="matrix-m1-dashboard"),
    path("api/v1/admin/cashback/policy-versions/<version>/", plain_view, name="matrix-m1-policy"),
    path("api/v1/admin/sessions/<uuid:sid>/", plain_view, name="matrix-m1-session"),
]


def test_matrix_fills_m1_route_kwargs() -> None:
    module: Any = ModuleType("m1_matrix_urls")
    module.urlpatterns = m1_urlpatterns
    try:
        with override_settings(ROOT_URLCONF=module):
            clear_url_caches()
            paths = {route_path for route_path, _ in protected_routes(get_resolver().url_patterns)}
    finally:
        clear_url_caches()
    assert paths == {
        "/api/v1/admin/categories/serums/",
        "/api/v1/admin/orders/1/transition/processing/",
        "/api/v1/admin/auth/oauth/google/",
        "/api/v1/admin/appointments/1/confirm/",
        "/api/v1/admin/dashboard/2026-10-26/",
        "/api/v1/admin/cashback/policy-versions/1/",
        "/api/v1/admin/sessions/00000000-0000-4000-8000-000000000001/",
    }


def test_matrix_failure_message_asks_for_unique_route_names() -> None:
    module: Any = ModuleType("ambiguous_matrix_urls")
    module.urlpatterns = [
        path("api/v1/admin/first/<str:code>/", plain_view, name="matrix-dup"),
        path("api/v1/admin/<str:code>/second/", plain_view, name="matrix-dup"),
    ]
    try:
        with override_settings(ROOT_URLCONF=module):
            clear_url_caches()
            with pytest.raises(pytest.fail.Exception, match="give the route a unique name"):
                list(protected_routes(get_resolver().url_patterns))
    finally:
        clear_url_caches()
