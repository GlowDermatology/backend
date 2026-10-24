from typing import Any
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import AuthSession, StaffUser
from glowderma.core.models import AuditEntry, IdempotencyRecord

pytestmark = pytest.mark.django_db
WRONG = "wrong"
BASE = "/api/v1/admin/auth/"
PASSWORD = "Saffron!Harbor-Long72"  # noqa: S105


@pytest.fixture
def user() -> StaffUser:
    return StaffUser.objects.create_user("admin@x.uz", PASSWORD, full_name="Administrator")


def login(client: APIClient, email: str = "admin@x.uz", password: str = PASSWORD) -> Any:
    return client.post(BASE + "login/", {"email": email, "password": password}, format="json")


def test_login_success_returns_tokens_and_audits(user: StaffUser) -> None:
    response = login(APIClient())
    assert response.status_code == 200
    assert set(response.data) == {"access", "access_expires_at", "refresh", "refresh_expires_at", "user"}
    assert set(response.data["user"]) == {
        "id",
        "email",
        "display_name",
        "is_superuser",
        "linked_providers",
        "session_expires_at",
    }
    assert response.data["user"]["display_name"] == "Administrator"
    assert response.data["access_expires_at"].endswith("+05:00")
    assert AuditEntry.objects.get(action="auth.login_succeeded").after == {
        "email": user.email,
        "method": "password",
    }


def test_login_email_is_case_insensitive(user: StaffUser) -> None:
    assert login(APIClient(), " Admin@X.uz ").status_code == 200


def test_login_bad_password_is_401_not_403(user: StaffUser) -> None:
    response = login(APIClient(), password=WRONG)
    assert response.status_code == 401
    assert response.data["code"] == "invalid_credentials"


def test_login_unknown_email_same_response_as_bad_password(user: StaffUser) -> None:
    wrong = login(APIClient(), password=WRONG).data
    unknown = login(APIClient(), "unknown@x.uz", "wrong").data
    assert {key: value for key, value in wrong.items() if key != "request_id"} == {
        key: value for key, value in unknown.items() if key != "request_id"
    }


def test_locked_account_rejects_correct_password(user: StaffUser) -> None:
    client = APIClient()
    for _ in range(5):
        assert login(client, password=WRONG).status_code == 401
    assert login(client).status_code == 429


def test_lockout_after_5_failures_also_for_unknown_email() -> None:
    client = APIClient()
    for _ in range(5):
        assert login(client, "unknown@x.uz", "wrong").status_code == 401
    response = login(client, "unknown@x.uz", "wrong")
    assert response.status_code == 429
    assert response.data["code"] == "throttled"
    assert int(response["Retry-After"]) >= 1


def test_lock_counter_resets_after_success(user: StaffUser) -> None:
    client = APIClient()
    for _ in range(4):
        login(client, password=WRONG)
    assert login(client).status_code == 200
    for _ in range(5):
        assert login(client, password=WRONG).status_code == 401
    assert login(client, password=WRONG).status_code == 429


def test_login_ip_throttle_ignores_spoofed_xff() -> None:
    client = APIClient()
    for index in range(31):
        response = client.post(
            BASE + "login/",
            {"email": f"user{index}@x.uz", "password": "wrong"},
            format="json",
            REMOTE_ADDR="8.8.8.8",
            HTTP_X_FORWARDED_FOR=f"1.2.3.{index}",
        )
    assert response.status_code == 429


def test_password_whitespace_preserved() -> None:
    StaffUser.objects.create_user("admin@x.uz", "  " + PASSWORD + "  ")
    assert login(APIClient(), password="  " + PASSWORD + "  ").status_code == 200
    assert login(APIClient()).status_code == 401


def test_login_ignores_idempotency_key(user: StaffUser) -> None:
    client = APIClient()
    client.credentials(HTTP_IDEMPOTENCY_KEY=str(uuid4()))
    assert login(client).status_code == 200
    assert login(client).status_code == 200
    assert AuthSession.objects.count() == 2
    assert IdempotencyRecord.objects.count() == 0


def test_refresh_returns_new_access_same_session(user: StaffUser) -> None:
    client = APIClient()
    initial = login(client).data
    response = client.post(BASE + "refresh/", {"refresh": initial["refresh"]}, format="json")
    assert response.status_code == 200
    assert set(response.data) == {"access", "access_expires_at", "refresh_expires_at"}
    assert response.data["access"] != initial["access"]
    assert response.data["refresh_expires_at"] == initial["refresh_expires_at"]
    assert AuthSession.objects.count() == 1


@pytest.mark.parametrize("scope,revoked", [("current", 1), ("others", 1), ("all", 2)])
def test_logout_scopes(
    user: StaffUser, scope: str, revoked: int, django_capture_on_commit_callbacks: Any
) -> None:
    client = APIClient()
    first, second = login(client).data, login(client).data
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(BASE + "logout/", {"refresh": first["refresh"], "scope": scope}, format="json")
    assert response.status_code == 204
    assert AuthSession.objects.filter(revoked_at__isnull=False).count() == revoked
    for label, data in (("current", first), ("other", second)):
        refreshed = client.post(BASE + "refresh/", {"refresh": data["refresh"]}, format="json")
        should_fail = (
            scope == "all"
            or (scope == "current" and label == "current")
            or (scope == "others" and label == "other")
        )
        assert refreshed.status_code == (401 if should_fail else 200)


def test_me_requires_staff_token(user: StaffUser) -> None:
    client = APIClient()
    assert client.get(BASE + "me/").status_code == 401
    data = login(client).data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
    response = client.get(BASE + "me/")
    assert response.status_code == 200
    assert response.data == data["user"]


def test_change_password_revokes_other_sessions(
    user: StaffUser, django_capture_on_commit_callbacks: Any
) -> None:
    client = APIClient()
    first, second = login(client).data, login(client).data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {first['access']}")
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            BASE + "change-password/",
            {"current_password": PASSWORD, "new_password": "Juniper!Harbor-73-new"},
            format="json",
        )
    assert response.status_code == 204
    assert client.get(BASE + "me/").status_code == 200
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {second['access']}")
    assert client.get(BASE + "me/").status_code == 401
    user.refresh_from_db()
    assert user.check_password("Juniper!Harbor-73-new")


@pytest.mark.parametrize(
    "current,new,field,code",
    [
        ("wrong", "Juniper!Harbor-73-new", "current_password", "password_incorrect"),
        (PASSWORD, PASSWORD, "new_password", "password_unchanged"),
        (PASSWORD, "12", "new_password", "password_too_short"),
    ],
)
def test_change_password_field_errors(user: StaffUser, current: str, new: str, field: str, code: str) -> None:
    client = APIClient()
    data = login(client).data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
    response = client.post(
        BASE + "change-password/", {"current_password": current, "new_password": new}, format="json"
    )
    assert response.status_code == 400
    assert response.data["errors"][0]["field"] == field
    assert response.data["errors"][0]["code"] == code


def test_inactive_login_is_invalid_credentials(user: StaffUser) -> None:
    user.is_active = False
    user.save(update_fields=["is_active"])
    response = login(APIClient())
    assert response.status_code == 401
    assert response.data["code"] == "invalid_credentials"


def test_unknown_refresh_and_logout_codes() -> None:
    client = APIClient()
    refresh = client.post(BASE + "refresh/", {"refresh": "unknown"}, format="json")
    logout = client.post(BASE + "logout/", {"refresh": "unknown"}, format="json")
    assert refresh.status_code == logout.status_code == 401
    assert refresh.data["code"] == "not_authenticated"
    assert logout.data["code"] == "SESSION_EXPIRED"
    invalid = client.post(BASE + "logout/", {"refresh": "unknown", "scope": "bad"}, format="json")
    assert invalid.status_code == 400
    assert invalid.data["errors"][0]["code"] == "invalid_choice"


def test_auth_rejects_form_content_type() -> None:
    response = APIClient().post(
        BASE + "login/", "email=admin%40x.uz&password=wrong", content_type="application/x-www-form-urlencoded"
    )
    assert response.status_code == 415
