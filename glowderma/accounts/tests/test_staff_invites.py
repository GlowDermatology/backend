"""feat/admin-ux staff invites and the forced password change (admin contract §2.1, §2.16; overrides GST-3):
the ``delivery`` modes of ``POST staff/`` and ``reset-password/``, ``StaffPasswordToken`` and the public
``auth/password-token/check|set/``, the ``IsActiveStaff`` gate, ``PATCH auth/me/``, the e-mail outbox topic,
the console refusal, the operator commands and the settings."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from rest_framework.test import APIClient

from glowderma.accounts import selectors, services, tasks, topics
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.errors import PASSWORD_TOKEN_INVALID
from glowderma.accounts.models import AuthSession, StaffPasswordToken, StaffUser
from glowderma.accounts.tests.conftest import bearer_client
from glowderma.accounts.tests.factories import TEST_PASSWORD, AuthSessionFactory, StaffUserFactory
from glowderma.core import enums, integration_modes, secretbox, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tests.test_settings import load_fresh_production, production_env  # noqa: F401 - fixtures
from glowderma.integrations.email import registry as email_registry
from glowderma.integrations.email.fake import FakeEmail
from glowderma.integrations.email.ports import EmailMessage, EmailUnavailable

pytestmark = pytest.mark.django_db

STAFF = "/api/v1/admin/staff/"
ME = "/api/v1/admin/auth/me/"
LOGIN = "/api/v1/admin/auth/login/"
CHANGE = "/api/v1/admin/auth/change-password/"
OWN_LINK = "/api/v1/admin/auth/password-link/"
CHECK = "/api/v1/admin/auth/password-token/check/"
SET = "/api/v1/admin/auth/password-token/set/"
NEW_PASSWORD = "Juniper!Canyon-Bright58"  # noqa: S105
PANEL = "https://panel.example.uz/admin"


@pytest.fixture(autouse=True)
def clean_fake_email() -> Any:
    email_registry.fake_email().sent.clear()
    yield
    email_registry.fake_email().sent.clear()


def key() -> dict[str, Any]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def link_message(token: StaffPasswordToken) -> OutboxMessage:
    return OutboxMessage.objects.get(dedupe_key=topics.staff_password_link_key(token.pk))


def raw_link(message: OutboxMessage) -> str:
    """The link as the handler opens it (tests only: the raw token exists nowhere else)."""
    payload = message.payload
    assert isinstance(payload, dict)
    return secretbox.open_(payload["link"], aad=services.password_link_aad(int(payload["link_id"]))).decode()


def token_of(message: OutboxMessage) -> str:
    return raw_link(message).split("token=", 1)[1]


def issue(user: StaffUser, actor: StaffUser | None = None, purpose: str = "invite") -> str:
    """A fresh link of ``user``; returns the raw token."""
    token = services.issue_password_link(user, purpose, actor=actor, via="resend")
    return token_of(link_message(token))


def pending_user(**fields: Any) -> StaffUser:
    return StaffUserFactory.create(must_change_password=True, **fields)


# --- create / reset deliveries ------------------------------------------------------------------------------


def test_temporary_password_delivery_forces_a_change_and_issues_no_link(manager: StaffUser) -> None:
    user, password = services.create_staff(email="nodira@x.uz", full_name="Nodira", actor=manager)
    assert password is not None and user.check_password(password)
    user.refresh_from_db()
    assert user.must_change_password is True and user.password_changed_at is None and user.language == "uz"
    assert not StaffPasswordToken.objects.exists()
    assert not OutboxMessage.objects.filter(topic=topics.STAFF_PASSWORD_LINK).exists()


def test_email_link_delivery_leaves_no_usable_password_and_seals_the_link(manager: StaffUser) -> None:
    user, password = services.create_staff(
        email="Nodira@X.uz", full_name="Nodira", actor=manager, delivery="email_link", language="ru"
    )
    assert password is None
    user.refresh_from_db()
    assert not user.has_usable_password() and user.must_change_password and user.language == "ru"
    token = StaffPasswordToken.objects.get(staff=user)
    assert (token.purpose, token.created_by_id, token.used_at, token.revoked_at) == (
        "invite",
        manager.pk,
        None,
        None,
    )
    assert timedelta(hours=71, minutes=59) < token.expires_at - token.created_at <= timedelta(hours=72)
    message = link_message(token)
    payload = message.payload
    assert isinstance(payload, dict)
    assert set(payload) == {"link_id", "to", "link", "name", "lang", "purpose"}
    assert (payload["lang"], payload["purpose"], payload["link_id"]) == ("ru", "invite", token.pk)
    raw = token_of(message)
    # neither the address, the name nor the token is readable in the stored payload or the audit log
    assert "nodira" not in str(payload).lower() and raw not in str(payload)
    assert len(raw) == 43 and token.token_hash == services.hash_secret(raw)
    assert raw_link(message) == f"http://localhost:3000/admin/set-password?token={raw}"
    created = AuditEntry.objects.get(action="staff.created")
    assert isinstance(created.after, dict)
    assert (created.after["delivery"], created.after["must_change_password"]) == ("email_link", True)
    sent = AuditEntry.objects.get(action="staff.password_link_sent")
    assert isinstance(sent.after, dict)
    assert (sent.after["purpose"], sent.after["via"], sent.after["link_id"]) == (
        "invite",
        "create",
        token.pk,
    )
    assert raw not in str(sent.after) and raw not in str(created.after)


def test_both_delivery_returns_the_password_and_sends_a_link(manager: StaffUser) -> None:
    user, password = services.create_staff(email="a@x.uz", full_name="Aziza", actor=manager, delivery="both")
    assert password is not None and user.check_password(password)
    assert StaffPasswordToken.objects.filter(staff=user, revoked_at__isnull=True).count() == 1


@pytest.mark.parametrize("delivery", ["email_link", "both"])
def test_email_delivery_is_a_field_error_without_the_capability(manager: StaffUser, delivery: str) -> None:
    client = bearer_client(manager)
    body = {"email": "a@x.uz", "full_name": "Aziza", "delivery": delivery}
    with override_settings(ADMIN_PANEL_URL=""):
        response = client.post(STAFF, body, **key())
        assert client.get(ME).data["capabilities"]["staff_email_invite"] is False
    assert response.status_code == 400
    [error] = response.data["errors"]
    assert (error["field"], error["code"]) == ("delivery", "invalid_choice")
    assert not StaffUser.objects.filter(email="a@x.uz").exists()


def test_fake_email_under_production_settings_disables_the_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert services.staff_email_invite_available() is True
    monkeypatch.setattr(integration_modes, "fake_in_production", lambda provider: provider == "email")
    assert services.staff_email_invite_available() is False


def test_create_api_answers_with_the_activation(manager_client: APIClient) -> None:
    response = manager_client.post(
        STAFF, {"email": "a@x.uz", "full_name": "Aziza", "delivery": "email_link"}, **key()
    )
    assert response.status_code == 201
    assert response.data["temporary_password"] is None
    assert response.data["activation"]["sent"] is True
    staff = response.data["staff"]
    assert staff["must_change_password"] is True and staff["password_changed_at"] is None
    assert staff["mfa_enabled"] is False
    assert staff["invite"]["state"] == "pending" and staff["invite"]["sent_at"] is None
    assert staff["invite"]["expires_at"] == response.data["activation"]["expires_at"]


def test_create_api_default_keeps_the_temporary_password_answer(manager_client: APIClient) -> None:
    response = manager_client.post(STAFF, {"email": "b@x.uz", "full_name": "Bobur"}, **key())
    assert response.status_code == 201
    assert response.data["activation"] is None and response.data["temporary_password"]
    assert response.data["staff"]["invite"] == {"state": "none", "sent_at": None, "expires_at": None}


def test_create_api_refuses_an_unknown_delivery_or_language(manager_client: APIClient) -> None:
    response = manager_client.post(
        STAFF, {"email": "c@x.uz", "full_name": "Cc", "delivery": "sms", "language": "de"}, **key()
    )
    assert response.status_code == 400
    assert {(e["field"], e["code"]) for e in response.data["errors"]} == {
        ("delivery", "invalid_choice"),
        ("language", "invalid_choice"),
    }


def test_reset_by_email_link_kills_the_password_sessions_and_earlier_links(manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    AuthSessionFactory.create(staff=target)
    old = services.send_password_link(target, actor=manager)
    response = bearer_client(manager).post(
        f"{STAFF}{target.pk}/reset-password/", {"delivery": "email_link"}, **key()
    )
    assert response.status_code == 200
    assert response.data["temporary_password"] is None and response.data["activation"]["sent"] is True
    target.refresh_from_db()
    assert not target.has_usable_password() and target.must_change_password
    assert not AuthSession.objects.filter(staff=target, revoked_at__isnull=True).exists()
    old.refresh_from_db()
    assert old.revoked_at is not None
    newest = StaffPasswordToken.objects.filter(staff=target).latest("created_at")
    assert newest.purpose == "reset" and newest.revoked_at is None
    entry = AuditEntry.objects.get(action="staff.password_reset")
    assert isinstance(entry.after, dict)
    assert (entry.after["delivery"], entry.after["generated"]) == ("email_link", False)


def test_reset_with_a_temporary_password_revokes_open_links(manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    token = services.send_password_link(target, actor=manager)
    password, _ = services.reset_staff_password(target, actor=manager)
    token.refresh_from_db()
    target.refresh_from_db()
    assert token.revoked_at is not None and password is not None and target.must_change_password


def test_deactivation_revokes_open_links(manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    raw = issue(target, manager)
    services.deactivate_staff(target, actor=manager)
    assert APIClient().post(CHECK, {"token": raw}, format="json").data["code"] == "token_invalid"
    assert StaffPasswordToken.objects.get(staff=target).revoked_at is not None


def test_a_new_link_revokes_the_previous_one(manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    first = issue(target, manager)
    second = issue(target, manager)
    assert APIClient().post(CHECK, {"token": first}, format="json").status_code == 400
    assert APIClient().post(CHECK, {"token": second}, format="json").status_code == 200


# --- the public check / set ---------------------------------------------------------------------------------


def test_check_tells_whose_link_it_is(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="malika@glowderma.uz", full_name="Malika")
    raw = issue(target, manager, purpose="reset")
    response = APIClient().post(CHECK, {"token": raw}, format="json")
    assert response.status_code == 200
    assert set(response.data) == {"email_masked", "full_name", "purpose", "expires_at"}
    assert (response.data["email_masked"], response.data["full_name"], response.data["purpose"]) == (
        "m***@glowderma.uz",
        "Malika",
        "reset",
    )


def test_set_password_with_a_link(manager: StaffUser, django_capture_on_commit_callbacks: Any) -> None:
    target = pending_user(email="t@x.uz")
    session = AuthSessionFactory.create(staff=target)
    raw = issue(target, manager)
    other = StaffPasswordToken.objects.create(
        staff=target, purpose="reset", token_hash="0" * 64, expires_at=timeutils.now() + timedelta(hours=1)
    )
    guard = LoginGuard()
    for _ in range(5):
        guard.record_failure("login", email_hash(target.email))
    with django_capture_on_commit_callbacks(execute=True):
        response = APIClient().post(SET, {"token": raw, "new_password": NEW_PASSWORD}, format="json")
    assert response.status_code == 204 and response.content == b""
    target.refresh_from_db()
    assert target.check_password(NEW_PASSWORD) and not target.must_change_password
    assert target.password_changed_at is not None
    used = StaffPasswordToken.objects.get(token_hash=services.hash_secret(raw))
    assert used.used_at is not None
    other.refresh_from_db()
    assert other.revoked_at is not None
    session.refresh_from_db()
    assert session.revoke_reason == "password_changed"
    entry = AuditEntry.objects.get(action="staff.password_set")
    assert (entry.actor_id, entry.entity_id) == (target.pk, str(target.pk))
    assert isinstance(entry.after, dict) and entry.after["revoked_count"] == 1
    assert raw not in str(entry.after) and NEW_PASSWORD not in str(AuditEntry.objects.values_list("after"))
    # no session opened; the login lockout ended; the person signs in with the new password
    assert not AuthSession.objects.filter(staff=target, revoked_at__isnull=True).exists()
    login = APIClient().post(LOGIN, {"email": target.email, "password": NEW_PASSWORD}, format="json")
    assert login.status_code == 200 and login.data["user"]["must_change_password"] is False
    # single use
    again = APIClient().post(SET, {"token": raw, "new_password": "Another!Canyon-Bright59"}, format="json")
    assert (again.status_code, again.data["code"]) == (400, "token_invalid")


def _invalid_tokens(manager: StaffUser) -> dict[str, str]:
    expired_user = StaffUserFactory.create()
    expired = issue(expired_user, manager)
    StaffPasswordToken.objects.filter(staff=expired_user).update(
        expires_at=timeutils.now() - timedelta(seconds=1)
    )
    revoked_user = StaffUserFactory.create()
    revoked = issue(revoked_user, manager)
    services.revoke_password_tokens(revoked_user)
    used_user = StaffUserFactory.create()
    used = issue(used_user, manager)
    services.set_password_with_token(used, NEW_PASSWORD)
    inactive_user = StaffUserFactory.create()
    inactive = issue(inactive_user, manager)
    StaffUser.objects.filter(pk=inactive_user.pk).update(is_active=False)
    return {
        "unknown": "x" * 43,
        "expired": expired,
        "revoked": revoked,
        "used": used,
        "inactive": inactive,
        "too_long": "y" * 129,
    }


def test_every_bad_link_gets_one_uniform_answer(manager: StaffUser) -> None:
    bodies = set()
    for kind, raw in _invalid_tokens(manager).items():
        for url, body in ((CHECK, {"token": raw}), (SET, {"token": raw, "new_password": NEW_PASSWORD})):
            response = APIClient().post(url, body, format="json")
            assert response.status_code == 400, (kind, url)
            if kind != "too_long":
                bodies.add((response.data["code"], response.data["message"], str(response.data["errors"])))
    assert bodies == {("token_invalid", PASSWORD_TOKEN_INVALID, "[]")}


def test_set_validates_the_password_for_the_links_account(manager: StaffUser) -> None:
    target = StaffUserFactory.create(email="saida@x.uz", full_name="Saida")
    raw = issue(target, manager)
    for password, code in (("short", "password_too_short"), (TEST_PASSWORD, "password_unchanged")):
        response = APIClient().post(SET, {"token": raw, "new_password": password}, format="json")
        assert response.status_code == 400
        assert ("new_password", code) in {(e["field"], e["code"]) for e in response.data["errors"]}
    token = StaffPasswordToken.objects.get(staff=target)
    assert token.used_at is None and token.revoked_at is None  # still usable after a refused password


def test_the_public_endpoints_are_throttled_per_ip() -> None:
    with override_settings(THROTTLE_RATES={"staff_password_token": "2/min"}):
        codes = [APIClient().post(CHECK, {"token": "z" * 43}, format="json").status_code for _ in range(3)]
    assert codes == [400, 400, 429]


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_sets_use_the_link_once(manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    _row, raw = services.issue_password_token(target, "invite", actor=manager)
    results: list[str] = []
    start = threading.Event()

    def attempt(password: str) -> None:
        try:
            start.wait(10)
            services.set_password_with_token(raw, password)
            results.append("ok")
        except Exception as exc:  # noqa: BLE001 - the refusal is the expected outcome of one thread
            results.append(type(exc).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(f"{NEW_PASSWORD}{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == ["PasswordTokenInvalid", "ok"]
    assert StaffPasswordToken.objects.get(staff=target).used_at is not None


# --- the forced change gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["password", "google"])
def test_a_pending_change_blocks_every_route_but_the_allow_list(method: str) -> None:
    user = pending_user()
    session = AuthSessionFactory.create(staff=user, auth_method=method)
    from glowderma.accounts import tokens

    access, _ = tokens.issue_access(session)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    blocked = client.get(STAFF)
    assert blocked.status_code == 403
    assert (blocked.data["code"], blocked.data["message"]) == (
        "password_change_required",
        "Avval parolingizni o‘zgartiring.",
    )
    assert client.get("/api/v1/admin/orders/").status_code == 403
    me = client.get(ME)
    assert me.status_code == 200 and me.data["must_change_password"] is True
    assert client.patch(ME, {"language": "en"}, format="json").data["language"] == "en"


def test_managers_and_superusers_are_gated_too(manager: StaffUser) -> None:
    StaffUser.objects.filter(pk=manager.pk).update(must_change_password=True, is_superuser=True)
    response = bearer_client(manager).get(f"{STAFF}{manager.pk}/")
    assert response.status_code == 403 and response.data["code"] == "password_change_required"


def test_change_password_clears_the_gate(django_capture_on_commit_callbacks: Any) -> None:
    user = pending_user()
    client = bearer_client(user)
    token = StaffPasswordToken.objects.create(
        staff=user, purpose="invite", token_hash="1" * 64, expires_at=timeutils.now() + timedelta(hours=1)
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(CHANGE, {"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 204
    user.refresh_from_db()
    assert not user.must_change_password and user.password_changed_at is not None
    token.refresh_from_db()
    assert token.revoked_at is not None
    entry = AuditEntry.objects.get(action="auth.password_changed")
    assert entry.after == {"forced": True}
    assert client.get(STAFF).status_code == 200  # the same session goes on


def test_an_unforced_change_is_audited_as_such(staff_client: APIClient) -> None:
    assert (
        staff_client.post(
            CHANGE, {"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    assert AuditEntry.objects.get(action="auth.password_changed").after == {"forced": False}


def test_logout_needs_no_bearer_and_works_while_pending() -> None:
    from glowderma.accounts.tests.factories import raw_refresh

    session = AuthSessionFactory.create(staff=pending_user())
    response = APIClient().post(
        "/api/v1/admin/auth/logout/", {"refresh": raw_refresh(session)}, format="json"
    )
    assert response.status_code == 204


# --- me / login ---------------------------------------------------------------------------------------------


def test_me_and_login_carry_the_new_fields(staff: StaffUser) -> None:
    login = APIClient().post(LOGIN, {"email": staff.email, "password": TEST_PASSWORD}, format="json")
    assert login.status_code == 200
    user = login.data["user"]
    assert (user["must_change_password"], user["language"], user["mfa_enabled"]) == (False, "uz", False)
    assert user["capabilities"]["staff_email_invite"] is True


def test_patch_me_validates_the_language(staff_client: APIClient, staff: StaffUser) -> None:
    response = staff_client.patch(ME, {"language": "de"}, format="json")
    assert response.status_code == 400 and response.data["errors"][0]["field"] == "language"
    assert staff_client.patch(ME, {}, format="json").status_code == 400
    response = staff_client.patch(ME, {"language": "ru"}, format="json")
    assert response.status_code == 200 and response.data["language"] == "ru"
    staff.refresh_from_db()
    assert staff.language == "ru"


# --- resend and the self link -------------------------------------------------------------------------------


def test_manager_resends_a_link(manager_client: APIClient, manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    first = manager_client.post(f"{STAFF}{target.pk}/send-password-link/")
    assert first.status_code == 200 and first.data["activation"]["sent"] is True
    assert first.data["staff"]["invite"]["state"] == "pending"
    assert StaffPasswordToken.objects.get(staff=target).purpose == "invite"  # never signed in
    AuthSessionFactory.create(staff=target)
    assert manager_client.post(f"{STAFF}{target.pk}/send-password-link/").status_code == 200
    tokens = list(StaffPasswordToken.objects.filter(staff=target).order_by("created_at", "id"))
    assert [t.purpose for t in tokens] == ["invite", "reset"]
    assert tokens[0].revoked_at is not None and tokens[1].revoked_at is None
    target.refresh_from_db()
    assert target.check_password(TEST_PASSWORD) and not target.must_change_password  # password untouched
    entry = AuditEntry.objects.filter(action="staff.password_link_sent").latest("id")
    assert isinstance(entry.after, dict) and entry.after["via"] == "resend"


def test_resend_refusals(manager_client: APIClient, manager: StaffUser, staff_client: APIClient) -> None:
    inactive = StaffUserFactory.create(is_active=False)
    target = StaffUserFactory.create()
    assert manager_client.post(f"{STAFF}{manager.pk}/send-password-link/").status_code == 409
    assert manager_client.post(f"{STAFF}{inactive.pk}/send-password-link/").status_code == 409
    assert manager_client.post(f"{STAFF}999999/send-password-link/").status_code == 404
    assert staff_client.post(f"{STAFF}{target.pk}/send-password-link/").status_code == 403
    with override_settings(ADMIN_PANEL_URL=""):
        assert manager_client.post(f"{STAFF}{target.pk}/send-password-link/").status_code == 409
    assert not StaffPasswordToken.objects.exists()


def test_resend_is_throttled_per_manager_and_target(manager_client: APIClient) -> None:
    target, other = StaffUserFactory.create(), StaffUserFactory.create()
    with override_settings(THROTTLE_RATES={"staff_password_link": "2/hour"}):
        codes = [manager_client.post(f"{STAFF}{target.pk}/send-password-link/").status_code for _ in range(3)]
        assert manager_client.post(f"{STAFF}{other.pk}/send-password-link/").status_code == 200
    assert codes == [200, 200, 429]


def test_own_link_for_a_pending_account(django_capture_on_commit_callbacks: Any) -> None:
    user = pending_user(email="oauth@glowderma.uz")
    client = bearer_client(user)
    response = client.post(OWN_LINK)
    assert response.status_code == 200
    assert response.data["email_masked"] == "o***@glowderma.uz" and response.data["activation"]["sent"]
    token = StaffPasswordToken.objects.get(staff=user)
    assert (token.purpose, token.created_by_id) == ("reset", user.pk)
    entry = AuditEntry.objects.get(action="staff.password_link_sent")
    assert isinstance(entry.after, dict) and (entry.actor_id, entry.after["via"]) == (user.pk, "self")


def test_own_link_refused_without_a_pending_change(staff_client: APIClient) -> None:
    response = staff_client.post(OWN_LINK)
    assert response.status_code == 409 and response.data["code"] == "conflict"


def test_own_link_is_throttled() -> None:
    client = bearer_client(pending_user())
    with override_settings(THROTTLE_RATES={"staff_password_link": "1/hour"}):
        assert [client.post(OWN_LINK).status_code for _ in range(2)] == [200, 429]


# --- StaffMember.invite -------------------------------------------------------------------------------------


def test_invite_states(manager: StaffUser) -> None:
    target = StaffUserFactory.create()

    def state() -> str:
        member = selectors.staff_member(target.pk)
        assert member is not None
        return selectors.invite_of(member).state

    assert state() == "none"
    raw = issue(target, manager)
    assert state() == "pending"
    StaffPasswordToken.objects.filter(staff=target).update(expires_at=timeutils.now() - timedelta(seconds=1))
    assert state() == "expired"
    raw = issue(target, manager)
    services.set_password_with_token(raw, NEW_PASSWORD)
    assert state() == "used"
    issue(target, manager)
    services.revoke_password_tokens(target)
    assert state() == "revoked"


def test_the_list_shows_the_password_state_to_managers_only(
    manager_client: APIClient, staff_client: APIClient, manager: StaffUser
) -> None:
    target = pending_user(full_name="Zebo")
    issue(target, manager)
    rows = {row["id"]: row for row in manager_client.get(STAFF).data["results"]}
    assert rows[target.pk]["must_change_password"] is True
    assert rows[target.pk]["invite"]["state"] == "pending"
    plain = {row["id"]: row for row in staff_client.get(STAFF).data["results"]}
    assert plain[target.pk]["must_change_password"] is None and plain[target.pk]["invite"] is None


# --- the e-mail handler -------------------------------------------------------------------------------------


def _link_email(manager: StaffUser, **fields: Any) -> tuple[StaffPasswordToken, OutboxMessage]:
    target = StaffUserFactory.create(**fields)
    token = services.issue_password_link(target, "invite", actor=manager, via="create")
    return token, link_message(token)


@pytest.mark.parametrize(
    ("lang", "subject", "phrase"),
    [
        ("uz", "Glow Derma: admin panelga taklif", "Havola 72 soat amal qiladi"),
        ("ru", "Glow Derma: приглашение в админ-панель", "Ссылка действует 72 ч"),
        ("en", "Glow Derma: your admin panel invitation", "The link is valid for 72 h"),
    ],
)
def test_the_handler_sends_the_link_in_the_staff_language(
    manager: StaffUser, lang: str, subject: str, phrase: str, caplog: pytest.LogCaptureFixture
) -> None:
    token, message = _link_email(manager, email="dilnoza@x.uz", full_name="Dilnoza", language=lang)
    link = raw_link(message)
    caplog.set_level(logging.DEBUG)
    tasks.send_staff_password_link(message)
    [sent] = email_registry.fake_email().sent
    assert (sent.to, sent.subject, sent.reference) == ("dilnoza@x.uz", subject, f"outbox_id={message.pk}")
    assert link in sent.text and link in sent.html and phrase in sent.text and "Dilnoza" in sent.text
    token.refresh_from_db()
    assert token.sent_at is not None
    message.refresh_from_db()
    assert isinstance(message.payload, dict)
    assert (message.payload["to"], message.payload["link"], message.payload["name"]) == ("", "", "")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "dilnoza" not in logged.lower() and token_of_link(link) not in logged
    # a redelivery sends nothing more
    tasks.send_staff_password_link(message)
    assert len(email_registry.fake_email().sent) == 1


def token_of_link(link: str) -> str:
    return link.split("token=", 1)[1]


def test_a_superseded_link_is_not_sent(manager: StaffUser) -> None:
    token, message = _link_email(manager)
    services.revoke_password_tokens(token.staff)
    tasks.send_staff_password_link(message)
    assert email_registry.fake_email().sent == []
    message.refresh_from_db()
    assert isinstance(message.payload, dict) and message.payload["link"] == ""


def test_relay_down_unclaims_and_retries(manager: StaffUser) -> None:
    token, message = _link_email(manager, email="x@down.test")
    with pytest.raises(EmailUnavailable):
        tasks.send_staff_password_link(message)
    token.refresh_from_db()
    message.refresh_from_db()
    assert token.sent_at is None and isinstance(message.payload, dict) and message.payload["link"]


def test_a_refused_address_is_final_and_unsent(manager: StaffUser) -> None:
    token, message = _link_email(manager, email="x@rejected.test")
    tasks.send_staff_password_link(message)
    token.refresh_from_db()
    message.refresh_from_db()
    assert token.sent_at is None and isinstance(message.payload, dict) and message.payload["link"] == ""


def test_an_unknown_outcome_keeps_the_claim(manager: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    token, message = _link_email(manager)

    def unknown(self: FakeEmail, email: EmailMessage) -> str:
        raise EmailUnavailable("timeout", sent=True)

    monkeypatch.setattr(FakeEmail, "send", unknown)
    tasks.send_staff_password_link(message)
    token.refresh_from_db()
    assert token.sent_at is not None


def test_a_broken_payload_and_a_dead_message_keep_nothing(manager: StaffUser) -> None:
    token, message = _link_email(manager)
    assert isinstance(message.payload, dict)
    broken = {**message.payload, "link_id": token.pk + 1000}  # the AAD no longer matches
    OutboxMessage.objects.filter(pk=message.pk).update(payload=broken)
    message.refresh_from_db()
    tasks.send_staff_password_link(message)
    message.refresh_from_db()
    assert isinstance(message.payload, dict) and message.payload["to"] == ""
    _token2, dead = _link_email(manager)
    tasks.blank_dead_staff_password_link(dead)
    dead.refresh_from_db()
    assert isinstance(dead.payload, dict) and (dead.payload["to"], dead.payload["link"]) == ("", "")
    assert email_registry.fake_email().sent == []


def test_cleanup_purges_old_link_messages(manager: StaffUser) -> None:
    _token, message = _link_email(manager)
    OutboxMessage.objects.filter(pk=message.pk).update(
        status="done", updated_at=timeutils.now() - timedelta(hours=25)
    )
    tasks.cleanup_auth()
    assert not OutboxMessage.objects.filter(pk=message.pk).exists()


# --- the ops console ----------------------------------------------------------------------------------------


def test_the_console_refuses_a_pending_change() -> None:
    from glowderma.accounts.tests.ops_helpers import ops_staff

    user = ops_staff("pending@x.uz")
    StaffUser.objects.filter(pk=user.pk).update(must_change_password=True)
    client = Client()
    response = client.post("/django-admin/login/", {"username": user.email, "password": TEST_PASSWORD})
    assert response.status_code == 200
    assert "Avval admin panelda o‘z parolingizni o‘rnating" in response.content.decode()
    assert client.get("/django-admin/").status_code == 302
    entry = AuditEntry.objects.get(action="console.login_failed")
    assert (entry.reason, entry.entity_id) == ("password_change_required", "")
    # the right password is not a failed guess: no lockout grows
    assert not AuditEntry.objects.filter(action="console.password_verified").exists()
    StaffUser.objects.filter(pk=user.pk).update(must_change_password=False)
    assert (
        client.post("/django-admin/login/", {"username": user.email, "password": TEST_PASSWORD}).status_code
        == 302
    )


# --- operator commands --------------------------------------------------------------------------------------


def _run(*args: Any, stdin: str, **options: Any) -> str:
    out = StringIO()
    with patch("sys.stdin", StringIO(stdin)):
        call_command(*args, stdout=out, password_stdin=True, **options)
    return out.getvalue()


def test_createstaff_must_change_flag() -> None:
    _run("createstaff", "plain@x.uz", stdin="Chosen!Harbor-Long-74\n")
    _run("createstaff", "forced@x.uz", stdin="Chosen!Harbor-Long-74\n", must_change=True)
    flags = dict(StaffUser.objects.values_list("email", "must_change_password"))
    assert flags == {"plain@x.uz": False, "forced@x.uz": True}


def test_resetstaffpassword_must_change_flag(staff: StaffUser) -> None:
    def flag() -> bool:
        return bool(StaffUser.objects.get(pk=staff.pk).must_change_password)

    _run("resetstaffpassword", staff.email, stdin="Chosen!Harbor-Long-74\n")
    assert flag() is False
    _run("resetstaffpassword", staff.email, stdin="Chosen!Harbor-Long-75\n", must_change=True)
    assert flag() is True
    StaffUser.objects.filter(pk=staff.pk).update(must_change_password=False)
    out = StringIO()
    call_command("resetstaffpassword", staff.email, stdout=out)  # generated: always forced
    assert flag() is True and "Temporary password" in out.getvalue()


# --- settings -----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("production_env")
@pytest.mark.parametrize(
    ("value", "ok"),
    [("https://glowderma.uz/admin", True), ("http://glowderma.uz/admin", False), ("", True)],
)
def test_production_requires_an_https_admin_panel_url(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, value: str, ok: bool
) -> None:
    load = request.getfixturevalue("load_fresh_production")
    monkeypatch.setenv("ADMIN_PANEL_URL", value)
    if ok:
        assert load().ADMIN_PANEL_URL == value
    else:
        with pytest.raises(ImproperlyConfigured, match="ADMIN_PANEL_URL"):
            load()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ADMIN_PANEL_URL", "glowderma.uz/admin"),
        ("ADMIN_PANEL_URL", "https://x.uz/admin?a=1"),
        ("STAFF_INVITE_TTL_HOURS", "0"),
        ("STAFF_INVITE_TTL_HOURS", "169"),
    ],
)
@pytest.mark.usefixtures("production_env")
def test_base_settings_refuse_bad_values(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, name: str, value: str
) -> None:
    load = request.getfixturevalue("load_fresh_production")
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=name):
        load()


def test_the_ttl_setting_drives_the_expiry(manager: StaffUser) -> None:
    target = StaffUserFactory.create()
    with override_settings(STAFF_INVITE_TTL_HOURS=1, ADMIN_PANEL_URL=PANEL):
        token = services.issue_password_link(target, "invite", actor=manager, via="resend")
        assert raw_link(link_message(token)).startswith(f"{PANEL}/set-password?token=")
    assert token.expires_at - token.created_at <= timedelta(hours=1)


def test_the_new_enums_are_contract_enums() -> None:
    assert enums.StaffPasswordDelivery.values == ["temporary_password", "email_link", "both"]
    assert enums.StaffInviteState.values == ["none", "pending", "expired", "used", "revoked"]
