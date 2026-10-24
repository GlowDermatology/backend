import re
from base64 import b64encode
from collections.abc import Iterator
from typing import Any

import pytest
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIClient

from glowderma.accounts import services, tokens
from glowderma.accounts.models import StaffUser


def protected_routes(patterns: list[Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    for pattern in patterns:
        segment = re.sub(
            r"<(?:(\w+):)?(\w+)>",
            lambda match: (
                "00000000-0000-4000-8000-000000000001"
                if match.group(1) == "uuid"
                else "1"
                if match.group(1) == "int"
                else "example"
            ),
            str(pattern.pattern),
        )
        path = prefix + segment
        if isinstance(pattern, URLResolver):
            yield from protected_routes(pattern.url_patterns, path)
        elif isinstance(pattern, URLPattern) and path.startswith("api/v1/admin/"):
            view_class = getattr(pattern.callback, "cls", None)
            methods = getattr(view_class, "http_method_names", [])
            actions = getattr(pattern.callback, "actions", {})
            for method in methods:
                if method in {"head", "options"} or not hasattr(view_class, actions.get(method, method)):
                    continue
                if (path, method) in {
                    (f"api/v1/admin/auth/{name}/", "post") for name in ("login", "refresh", "logout")
                }:
                    continue
                yield "/" + path, method


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
    for path, method in routes:
        for header in (None, f"Basic {basic}", f"Bearer {customer_token}", f"Bearer {revoked_token}"):
            client = APIClient()
            if header:
                client.credentials(HTTP_AUTHORIZATION=header)
            assert getattr(client, method)(path, {}, format="json").status_code == 401
        session_client = APIClient()
        session_client.force_login(user)
        assert getattr(session_client, method)(path, {}, format="json").status_code == 401
