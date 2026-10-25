"""The shared test infrastructure (factories and root conftest fixtures) works end to end."""

from hashlib import sha256

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import services, tokens
from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.accounts.tests.factories import (
    TEST_PASSWORD,
    AuthSessionFactory,
    StaffUserFactory,
    raw_refresh,
)

pytestmark = pytest.mark.django_db
BASE = "/api/v1/admin/auth/"


def test_staff_user_factory_sequences_and_password() -> None:
    first, second = StaffUserFactory.create(), StaffUserFactory.create()
    assert first.email != second.email
    assert first.full_name and first.full_name != second.full_name
    assert first.is_active and not first.is_staff
    assert first.check_password(TEST_PASSWORD)
    other = "Other!Secret-Long-99"  # noqa: S105
    custom = StaffUserFactory.create(email="Custom@X.uz", password=other, is_staff=True)
    custom.refresh_from_db()
    assert custom.email == "custom@x.uz"
    assert custom.is_staff and custom.check_password(other)


def test_auth_session_factory_uses_the_service() -> None:
    session = AuthSessionFactory.create()
    assert session.audience == "staff" and session.staff is not None
    assert session.refresh_hash == sha256(raw_refresh(session).encode()).hexdigest()
    assert services.refresh_staff_session(raw_refresh(session)).pk == session.pk


def test_staff_fixtures(staff: StaffUser, staff_session: tuple[AuthSession, str], staff_token: str) -> None:
    session, refresh = staff_session
    assert session.staff_id == staff.pk
    assert services.refresh_staff_session(refresh).pk == session.pk
    claims = tokens.decode_access(staff_token, audience="staff")
    assert (claims["sub"], claims["sid"]) == (str(staff.pk), str(session.pk))


def test_admin_client_is_authenticated_json_client(staff: StaffUser, admin_client: APIClient) -> None:
    response = admin_client.get(BASE + "me/")
    assert response.status_code == 200
    assert response.data["id"] == staff.pk
    # Default JSON format: a dict body is accepted where form data would be 415.
    response = admin_client.post(BASE + "change-password/", {"current_password": "x", "new_password": "y"})
    assert response.status_code == 400
    assert response.data["code"] == "validation_error"


def test_admin_client_sends_internal_secret(admin_client: APIClient) -> None:
    """§1.2: `X-GD-Client-IP` is honoured only when the internal secret matches."""
    response = admin_client.get(BASE + "me/", HTTP_X_GD_CLIENT_IP="198.51.100.7")
    assert response.status_code == 200
    assert response.wsgi_request.client_ip == "198.51.100.7"  # type: ignore[attr-defined]


def test_anon_client(anon_client: APIClient) -> None:
    response = anon_client.get(BASE + "me/")
    assert response.status_code == 401
    assert response.data["code"] == "not_authenticated"
    assert anon_client.post(BASE + "login/", {"email": "nobody@x.uz", "password": "x"}).status_code == 401
