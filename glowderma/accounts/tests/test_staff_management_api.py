"""Contract §2.16 staff management (decisions GST-1..GST-5): ``POST staff/``, ``GET`` / ``PATCH
staff/{id}/``, ``deactivate/``, ``reactivate/``, ``reset-password/`` — manager only, idempotent, audited, the
temporary password shown once and stored sealed."""

import re
import uuid
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import services
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.tests.conftest import bearer_client
from glowderma.accounts.tests.factories import TEST_PASSWORD, AuthSessionFactory, StaffUserFactory
from glowderma.core.idempotency import SEALED_MARK
from glowderma.core.models import AuditEntry, IdempotencyRecord

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/staff/"
LOGIN = "/api/v1/admin/auth/login/"
TEMPORARY = re.compile(r"^[A-Za-z2-9]{4}(-[A-Za-z2-9]{4}){3}$")
MEMBER_KEYS = {
    "id",
    "display_name",
    "email",
    "full_name",
    "is_active",
    "is_manager",
    "last_login_at",
    "linked_providers",
    "created_at",
}


def key() -> dict[str, Any]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def detail(pk: int, action: str = "") -> str:
    return f"{URL}{pk}/{action + '/' if action else ''}"


def can_sign_in(email: str, password: str) -> bool:
    return APIClient().post(LOGIN, {"email": email, "password": password}, format="json").status_code == 200


# --- permissions --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", ""),
        ("get", "{pk}"),
        ("patch", "{pk}"),
        ("post", "{pk}/deactivate"),
        ("post", "{pk}/reactivate"),
        ("post", "{pk}/reset-password"),
    ],
)
def test_only_managers_manage_staff(
    staff_client: APIClient, staff: StaffUser, method: str, path: str
) -> None:
    target = StaffUserFactory.create()
    url = URL + (path.format(pk=target.pk) + "/" if path else "")
    response = getattr(staff_client, method)(url, {}, **key())
    assert response.status_code == 403
    assert (response.data["code"], response.data["message"]) == ("permission_denied", "Ruxsat yo‘q.")
    target.refresh_from_db()
    assert target.is_active and target.check_password(TEST_PASSWORD)
    assert not AuditEntry.objects.filter(action__startswith="staff.").exists()


def test_a_removed_manager_role_applies_on_the_next_request(manager: StaffUser) -> None:
    client = bearer_client(manager)
    assert client.get(detail(manager.pk)).status_code == 200
    StaffUser.objects.filter(pk=manager.pk).update(is_manager=False)
    assert client.get(detail(manager.pk)).status_code == 403
    rows = client.get(URL).data["results"]
    assert all(row["is_manager"] is None for row in rows)


# --- create -------------------------------------------------------------------------------------------------


def test_create_returns_the_account_and_a_temporary_password_once(
    manager_client: APIClient, manager: StaffUser
) -> None:
    body = {"email": " Nodira@GlowDerma.uz ", "full_name": " Nodira Rahimova ", "is_manager": False}
    headers = key()
    response = manager_client.post(URL, body, **headers)
    assert response.status_code == 201, response.data
    assert set(response.data) == {"staff", "temporary_password"}
    staff, password = response.data["staff"], response.data["temporary_password"]
    assert set(staff) == MEMBER_KEYS
    user = StaffUser.objects.get(email="nodira@glowderma.uz")
    assert staff == {
        "id": user.pk,
        "display_name": "Nodira Rahimova",
        "email": "nodira@glowderma.uz",
        "full_name": "Nodira Rahimova",
        "is_active": True,
        "is_manager": False,
        "last_login_at": None,
        "linked_providers": [],
        "created_at": staff["created_at"],
    }
    assert TEMPORARY.fullmatch(password)
    assert can_sign_in("nodira@glowderma.uz", password)
    entry = AuditEntry.objects.get(action="staff.created")
    assert (entry.actor_id, entry.entity_id) == (manager.pk, str(user.pk))
    assert password not in str(entry.after)

    # The stored replay body keeps the password sealed; a replay opens it.
    record = IdempotencyRecord.objects.get()
    stored: Any = record.response_body
    assert password not in str(stored)
    assert set(stored["temporary_password"]) == {SEALED_MARK}
    replay = manager_client.post(URL, body, **headers)
    assert replay.status_code == 201 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.data == response.data
    assert StaffUser.objects.filter(email="nodira@glowderma.uz").count() == 1
    reused = manager_client.post(URL, {**body, "full_name": "Boshqa"}, **headers)
    assert reused.status_code == 422 and reused.data["code"] == "idempotency_key_reused"


def test_create_a_manager(manager_client: APIClient) -> None:
    response = manager_client.post(
        URL, {"email": "boss@x.uz", "full_name": "Boss", "is_manager": True}, **key()
    )
    assert response.status_code == 201 and response.data["staff"]["is_manager"] is True


def test_create_requires_an_idempotency_key(manager_client: APIClient) -> None:
    response = manager_client.post(URL, {"email": "a@x.uz", "full_name": "Ali"})
    assert response.status_code == 400 and response.data["code"] == "idempotency_key_required"
    assert StaffUser.objects.filter(email="a@x.uz").count() == 0


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, [("email", "required", "Majburiy maydon."), ("full_name", "required", "Majburiy maydon.")]),
        ({"email": "not-an-email", "full_name": "Ali"}, [("email", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"email": "", "full_name": "Ali"}, [("email", "blank", "Maydonni to‘ldiring.")]),
        (
            {"email": "a@x.uz", "full_name": " A "},
            [("full_name", "min_length", "Kamida 2 ta belgi kiriting.")],
        ),
        (
            {"email": "a@x.uz", "full_name": "x" * 151},
            [("full_name", "max_length", "150 belgidan oshmasin.")],
        ),
        (
            {"email": "a@x.uz", "full_name": "Ali", "is_manager": "maybe"},
            [("is_manager", "invalid", "Noto‘g‘ri qiymat.")],
        ),
    ],
)
def test_create_validation(manager_client: APIClient, body: dict[str, Any], expected: list[Any]) -> None:
    response = manager_client.post(URL, body, **key())
    assert response.status_code == 400
    assert errors(response) == expected


def test_create_refuses_an_email_in_use(manager_client: APIClient) -> None:
    StaffUserFactory.create(email="old@glowderma.uz", is_active=False)
    response = manager_client.post(URL, {"email": "OLD@glowderma.uz", "full_name": "Yangi"}, **key())
    assert response.status_code == 400
    assert errors(response) == [("email", "unique", services.STAFF_EMAIL_TAKEN)]


# --- retrieve and update ------------------------------------------------------------------------------------


def test_retrieve(manager_client: APIClient, staff: StaffUser) -> None:
    response = manager_client.get(detail(staff.pk))
    assert response.status_code == 200 and set(response.data) == MEMBER_KEYS
    assert response.data["email"] == staff.email
    assert manager_client.get(detail(staff.pk + 1000)).status_code == 404


def test_update_name_and_role(manager_client: APIClient, manager: StaffUser, staff: StaffUser) -> None:
    response = manager_client.patch(detail(staff.pk), {"full_name": "  Yangi Ism ", "is_manager": True})
    assert response.status_code == 200, response.data
    assert (response.data["full_name"], response.data["display_name"], response.data["is_manager"]) == (
        "Yangi Ism",
        "Yangi Ism",
        True,
    )
    entry = AuditEntry.objects.get(action="staff.updated")
    assert (entry.actor_id, entry.after) == (manager.pk, {"full_name": "Yangi Ism", "is_manager": True})
    # Nothing changes: nothing is written.
    assert manager_client.patch(detail(staff.pk), {"full_name": "Yangi Ism"}).status_code == 200
    assert manager_client.patch(detail(staff.pk), {}).status_code == 200
    assert AuditEntry.objects.filter(action="staff.updated").count() == 1


def test_update_refuses_read_only_keys(manager_client: APIClient, staff: StaffUser) -> None:
    response = manager_client.patch(
        detail(staff.pk), {"email": "new@x.uz", "is_active": False, "password": "x", "full_name": "A"}
    )
    assert response.status_code == 400
    assert errors(response) == [
        ("full_name", "min_length", "Kamida 2 ta belgi kiriting."),
        ("email", "read_only", "Bu maydonni o‘zgartirib bo‘lmaydi."),
        ("is_active", "read_only", "Bu maydonni o‘zgartirib bo‘lmaydi."),
        ("password", "read_only", "Bu maydonni o‘zgartirib bo‘lmaydi."),
    ]
    staff.refresh_from_db()
    assert staff.email != "new@x.uz"


def test_a_manager_cannot_remove_their_own_role(manager_client: APIClient, manager: StaffUser) -> None:
    StaffUserFactory.create(is_manager=True)
    response = manager_client.patch(detail(manager.pk), {"is_manager": False})
    assert response.status_code == 409
    assert (response.data["code"], response.data["message"]) == ("conflict", services.STAFF_SELF_DEMOTE)


def test_update_unknown_staff_is_404(manager_client: APIClient) -> None:
    assert manager_client.patch(detail(999_999), {"full_name": "Ali"}).status_code == 404


# --- deactivate / reactivate --------------------------------------------------------------------------------


def test_deactivate_revokes_sessions_and_access(
    manager_client: APIClient, manager: StaffUser, staff: StaffUser, staff_client: APIClient
) -> None:
    SocialIdentity.objects.create(staff=staff, provider="google", subject="g-1", email_at_link=staff.email)
    assert staff_client.get("/api/v1/admin/auth/me/").status_code == 200
    response = manager_client.post(detail(staff.pk, "deactivate"), **key())
    assert response.status_code == 200, response.data
    assert response.data["is_active"] is False and response.data["linked_providers"] == ["google"]
    assert set(AuthSession.objects.filter(staff=staff).values_list("revoke_reason", flat=True)) == {
        "deactivated"
    }
    me = staff_client.get("/api/v1/admin/auth/me/")
    assert (me.status_code, me.data["code"]) == (401, "SESSION_EXPIRED")
    assert not can_sign_in(staff.email, TEST_PASSWORD)
    entry = AuditEntry.objects.get(action="staff.deactivated")
    assert (entry.actor_id, entry.entity_id, entry.after) == (
        manager.pk,
        str(staff.pk),
        {"is_active": False, "revoked_count": 1},
    )
    # Already inactive: 200 unchanged, nothing written.
    assert manager_client.post(detail(staff.pk, "deactivate"), **key()).status_code == 200
    assert AuditEntry.objects.filter(action="staff.deactivated").count() == 1


def test_deactivate_requires_an_idempotency_key(manager_client: APIClient, staff: StaffUser) -> None:
    response = manager_client.post(detail(staff.pk, "deactivate"))
    assert response.status_code == 400 and response.data["code"] == "idempotency_key_required"
    staff.refresh_from_db()
    assert staff.is_active


def test_a_manager_cannot_deactivate_themselves(manager_client: APIClient, manager: StaffUser) -> None:
    response = manager_client.post(detail(manager.pk, "deactivate"), **key())
    assert response.status_code == 409
    assert response.data["message"] == services.STAFF_SELF_DEACTIVATE
    manager.refresh_from_db()
    assert manager.is_active


def test_reactivate(manager_client: APIClient, manager: StaffUser) -> None:
    target = StaffUserFactory.create(is_active=False, is_manager=True)
    response = manager_client.post(detail(target.pk, "reactivate"))
    assert response.status_code == 200
    assert (response.data["is_active"], response.data["is_manager"]) == (True, True)
    assert can_sign_in(target.email, TEST_PASSWORD)
    assert AuditEntry.objects.get(action="staff.reactivated").actor_id == manager.pk
    assert manager_client.post(detail(target.pk, "reactivate")).status_code == 200  # already active
    assert AuditEntry.objects.filter(action="staff.reactivated").count() == 1


def test_unknown_staff_is_404_for_every_action(manager_client: APIClient) -> None:
    for action in ("deactivate", "reactivate", "reset-password"):
        assert manager_client.post(detail(999_999, action), **key()).status_code == 404


# --- reset password -----------------------------------------------------------------------------------------


def test_reset_password(manager_client: APIClient, manager: StaffUser, staff: StaffUser) -> None:
    AuthSessionFactory.create(staff=staff)
    guard = LoginGuard()
    for _ in range(5):
        guard.record_failure("login", email_hash(staff.email))
    headers = key()
    response = manager_client.post(detail(staff.pk, "reset-password"), **headers)
    assert response.status_code == 200, response.data
    password = response.data["temporary_password"]
    assert TEMPORARY.fullmatch(password) and set(response.data["staff"]) == MEMBER_KEYS
    assert not AuthSession.objects.filter(staff=staff, revoked_at__isnull=True).exists()
    assert not can_sign_in(staff.email, TEST_PASSWORD)
    assert can_sign_in(staff.email, password)  # the earlier lockout ended with the reset
    entry = AuditEntry.objects.get(action="staff.password_reset")
    assert (entry.actor_id, entry.after) == (manager.pk, {"revoked_count": 1, "generated": True})
    stored: Any = IdempotencyRecord.objects.get().response_body
    assert password not in str(stored)
    replay = manager_client.post(detail(staff.pk, "reset-password"), **headers)
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.data["temporary_password"] == password
    assert AuditEntry.objects.filter(action="staff.password_reset").count() == 1


def test_reset_password_requires_an_idempotency_key(manager_client: APIClient, staff: StaffUser) -> None:
    response = manager_client.post(detail(staff.pk, "reset-password"))
    assert response.status_code == 400 and response.data["code"] == "idempotency_key_required"


def test_a_manager_resets_their_own_password_with_change_password(
    manager_client: APIClient, manager: StaffUser
) -> None:
    response = manager_client.post(detail(manager.pk, "reset-password"), **key())
    assert response.status_code == 409
    assert response.data["message"] == services.STAFF_SELF_RESET
    manager.refresh_from_db()
    assert manager.check_password(TEST_PASSWORD)


def test_a_sealed_password_that_no_longer_opens_replays_as_null(
    manager_client: APIClient, staff: StaffUser, settings: Any
) -> None:
    headers = key()
    assert manager_client.post(detail(staff.pk, "reset-password"), **headers).status_code == 200
    settings.SECRETBOX_KEY = "b3RoZXIta2V5LW90aGVyLWtleS1vdGhlci1rZXktMzI"  # rotated (32 bytes)
    replay = manager_client.post(detail(staff.pk, "reset-password"), **headers)
    assert replay.status_code == 200 and replay.data["temporary_password"] is None


def test_me_reports_the_manager_role(staff_client: APIClient, manager_client: APIClient) -> None:
    def me(client: APIClient) -> dict[str, Any]:
        response = client.get("/api/v1/admin/auth/me/")
        assert response.status_code == 200
        return dict(response.data)

    assert me(staff_client)["is_manager"] is False
    assert me(manager_client)["is_manager"] is True
