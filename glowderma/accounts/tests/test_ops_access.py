"""Console access provisioning: ``manage.py ops_access`` and the manager-only 2FA reset action."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.console import twofactor
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.accounts.tests.ops_helpers import login_through_forms, ops_login, ops_staff
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db


def after(entry: AuditEntry) -> dict[str, Any]:
    assert isinstance(entry.after, dict)
    return entry.after


def run(*args: str) -> str:
    out = StringIO()
    call_command("ops_access", *args, stdout=out)
    return out.getvalue()


def test_grant_gives_console_access_and_is_audited() -> None:
    user = StaffUserFactory.create(email="new@x.uz")
    assert not user.is_staff
    assert "may now use the ops console" in run("grant", "--email", "NEW@x.uz")
    assert StaffUser.objects.get(pk=user.pk).is_staff
    entry = AuditEntry.objects.get(action="ops_access.granted")
    assert (entry.actor_type, entry.actor_id, entry.entity_id) == ("system", None, str(user.pk))
    assert entry.reason == "manage.py ops_access"
    assert "nothing changed" in run("grant", "--email", "new@x.uz")
    assert AuditEntry.objects.filter(action="ops_access.granted").count() == 1


def test_grant_refuses_a_deactivated_account_and_unknown_emails() -> None:
    StaffUserFactory.create(email="gone@x.uz", is_active=False)
    with pytest.raises(CommandError, match="deactivated"):
        run("grant", "--email", "gone@x.uz")
    with pytest.raises(CommandError, match="No staff account"):
        run("grant", "--email", "nobody@x.uz")


def test_grant_with_an_actor_audits_the_actor() -> None:
    boss = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    StaffUserFactory.create(email="new@x.uz")
    run("grant", "--email", "new@x.uz", "--actor", "boss@x.uz")
    entry = AuditEntry.objects.get(action="ops_access.granted")
    assert (entry.actor_type, entry.actor_id) == ("staff", boss.pk)


def test_revoke_removes_access_devices_and_console_sessions() -> None:
    user = ops_staff("ops@x.uz")
    twofactor.issue_recovery_codes(user)
    client = Client()
    login_through_forms(client, "ops@x.uz")
    pending = Client()
    pending.post("/django-admin/login/", {"username": "ops@x.uz", "password": "Saffron!Harbor-Long72"})
    other = ops_staff("other@x.uz")
    other_client = Client()
    ops_login(other_client, other)
    out = run("revoke", "--email", "ops@x.uz")
    assert "no longer has ops console access" in out and "2 session(s) ended" in out
    user.refresh_from_db()
    assert not user.is_staff
    assert not TOTPDevice.objects.filter(user=user).exists()
    assert not StaticDevice.objects.filter(user=user).exists()
    assert client.get("/django-admin/").status_code == 302
    assert pending.get("/django-admin/2fa/")["Location"] == "/django-admin/login/"
    assert other_client.get("/django-admin/").status_code == 200  # nobody else is signed out
    entry = AuditEntry.objects.get(action="ops_access.revoked")
    assert after(entry)["sessions"] == 2 and after(entry)["recovery_codes"] == 10
    remaining = [session.get_decoded().get(SESSION_KEY) for session in Session.objects.all()]
    assert str(user.pk) not in remaining


def test_reset_2fa_deletes_the_devices_so_the_next_login_enrols() -> None:
    user = ops_staff("ops@x.uz")
    twofactor.issue_recovery_codes(user)
    client = Client()
    login_through_forms(client, "ops@x.uz")
    assert "reset" in run("reset-2fa", "--email", "ops@x.uz")
    assert not TOTPDevice.objects.filter(user=user).exists()
    assert not StaticToken.objects.filter(device__user=user).exists()
    assert client.get("/django-admin/").status_code == 302  # the session is no longer verified
    fresh = Client()
    answer = fresh.post("/django-admin/login/", {"username": "ops@x.uz", "password": "Saffron!Harbor-Long72"})
    assert answer["Location"] == "/django-admin/2fa/enrol/"
    user.refresh_from_db()
    assert user.is_staff
    assert after(AuditEntry.objects.get(action="ops_access.2fa_reset"))["totp_devices"] == 1


def test_status_prints_the_access_state() -> None:
    user = ops_staff("ops@x.uz")
    twofactor.issue_recovery_codes(user)
    out = run("status", "--email", "ops@x.uz")
    assert "is_staff: True" in out and "totp_confirmed: True" in out and "recovery_codes_left: 10" in out
    assert not AuditEntry.objects.exists()


# --- the admin action --------------------------------------------------------------------------------------


def changelist() -> str:
    return reverse("admin:accounts_staffuser_changelist")


def test_only_managers_see_the_reset_action() -> None:
    plain = Client()
    ops_login(plain, ops_staff("plain@x.uz"))
    assert "reset_two_factor" not in plain.get(changelist()).content.decode()
    manager = Client()
    ops_login(manager, ops_staff("boss@x.uz", is_manager=True))
    assert "reset_two_factor" in manager.get(changelist()).content.decode()
    target = ops_staff("target@x.uz")
    refused = plain.post(
        changelist(), {"action": "reset_two_factor", "_selected_action": [target.pk], "post": "yes"}
    )
    assert refused.status_code in {200, 302}
    assert TOTPDevice.objects.filter(user=target).exists()


def test_the_reset_action_confirms_then_resets_and_audits() -> None:
    manager = ops_staff("boss@x.uz", is_manager=True)
    target = ops_staff("target@x.uz")
    twofactor.issue_recovery_codes(target)
    client = Client()
    ops_login(client, manager)
    confirm = client.post(changelist(), {"action": "reset_two_factor", "_selected_action": [target.pk]})
    assert confirm.status_code == 200
    assert "target@x.uz" in confirm.content.decode() and 'name="post" value="yes"' in confirm.content.decode()
    assert TOTPDevice.objects.filter(user=target).exists()
    done = client.post(
        changelist(),
        {"action": "reset_two_factor", "_selected_action": [target.pk], "post": "yes"},
        follow=True,
    )
    assert "1 ta xodimning 2FA qurilmasi o‘chirildi" in done.content.decode()
    assert not TOTPDevice.objects.filter(user=target).exists()
    assert not StaticDevice.objects.filter(user=target).exists()
    entry = AuditEntry.objects.get(action="ops_access.2fa_reset")
    assert (entry.actor_id, entry.entity_id) == (manager.pk, str(target.pk))
    assert entry.reason == "ops console: 2FA ni qayta o‘rnatish"


def test_a_manager_cannot_reset_their_own_2fa_from_the_console() -> None:
    manager = ops_staff("boss@x.uz", is_manager=True)
    client = Client()
    ops_login(client, manager)
    answer = client.post(
        changelist(),
        {"action": "reset_two_factor", "_selected_action": [manager.pk], "post": "yes"},
        follow=True,
    )
    assert "O‘z 2FA ingizni" in answer.content.decode()
    assert TOTPDevice.objects.filter(user=manager).exists()
    assert not AuditEntry.objects.filter(action="ops_access.2fa_reset").exists()


def test_recovery_tokens_are_never_shown_in_the_console(ops_client: Client, ops_user: StaffUser) -> None:
    twofactor.issue_recovery_codes(ops_user)
    device = StaticDevice.objects.get(user=ops_user)
    stored = list(StaticToken.objects.filter(device=device).values_list("token", flat=True))
    for url in (
        reverse("admin:otp_static_staticdevice_changelist"),
        reverse("admin:otp_static_staticdevice_change", args=[device.pk]),
    ):
        page = ops_client.get(url)
        assert page.status_code == 200
        assert not any(token in page.content.decode() for token in stored)
