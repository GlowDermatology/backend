from typing import Any

import jwt
import pytest
from django.conf import settings
from django.core.cache import cache
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.accounts import services, tokens
from glowderma.accounts.authentication import StaffJWTAuthentication
from glowderma.accounts.models import StaffUser
from glowderma.accounts.permissions import IsActiveStaff

pytestmark = pytest.mark.django_db


class StaffView(APIView):
    authentication_classes = [StaffJWTAuthentication]
    permission_classes = [IsActiveStaff]

    def get(self, request: Request) -> Response:
        return Response({"id": request.user.pk})


def request_with(header: str | None) -> Response:
    headers: dict[str, Any] = {"HTTP_AUTHORIZATION": header} if header is not None else {}
    return StaffView.as_view()(APIRequestFactory().get("/api/v1/admin/test/", **headers))


@pytest.mark.parametrize("header", ["Bearer abc", "Bearer a.b.c", "Bearer", "Bearer a b"])
def test_malformed_bearer_is_401(header: str) -> None:
    response = request_with(header)
    assert response.status_code == 401
    assert response.data["code"] == "not_authenticated"


def test_access_without_jti_is_401() -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, _ = tokens.issue_access(session)
    claims = tokens.decode_access(token, audience="staff")
    claims.pop("jti")
    invalid = jwt.encode(
        claims,
        settings.JWT_SIGNING_KEYS[settings.JWT_ACTIVE_KID],
        algorithm="HS256",
        headers={"kid": settings.JWT_ACTIVE_KID},
    )
    assert request_with(f"Bearer {invalid}").status_code == 401


def test_inactive_staff_rejected_immediately() -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, _ = tokens.issue_access(session)
    assert request_with(f"Bearer {token}").status_code == 200
    StaffUser.objects.filter(pk=user.pk).update(is_active=False)
    response = request_with(f"Bearer {token}")
    assert response.status_code == 401
    assert response.data["code"] == "SESSION_EXPIRED"


def test_unauthenticated_admin_request_is_401_not_403() -> None:
    assert request_with(None).status_code == 401


def test_revoked_session_rejected_next_request(django_capture_on_commit_callbacks: Any) -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, _ = tokens.issue_access(session)
    assert request_with(f"Bearer {token}").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        services.revoke_session(session, "logout")
    response = request_with(f"Bearer {token}")
    assert response.status_code == 401
    assert response.data["code"] == "SESSION_EXPIRED"


def test_stale_fill_cannot_resurrect_revoked_session(
    monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    token, _ = tokens.issue_access(session)
    original_add = cache.add

    def add_after_revoke(key: str, value: Any, timeout: Any = None, **kwargs: Any) -> bool:
        with django_capture_on_commit_callbacks(execute=True):
            services.revoke_session(session, "logout")
        return bool(original_add(key, value, timeout=timeout, **kwargs))

    monkeypatch.setattr(cache, "add", add_after_revoke)
    request_with(f"Bearer {token}")
    assert cache.get(f"auth:sid:{session.pk}") == "revoked:logout"
    response = request_with(f"Bearer {token}")
    assert response.status_code == 401
    assert response.data["code"] == "SESSION_EXPIRED"
