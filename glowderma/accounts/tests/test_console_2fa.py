"""The ops console sign-in: password → TOTP (or enrolment → recovery codes) → verified session."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import SESSION_KEY
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.api.guard import email_hash
from glowderma.accounts.console import state, twofactor
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, StaffUserFactory
from glowderma.accounts.tests.ops_helpers import (
    KNOWN_TOTP_KEY,
    login_through_forms,
    ops_staff,
    totp_code,
    totp_code_for_key,
)
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db


def after(entry: AuditEntry) -> dict[str, Any]:
    assert isinstance(entry.after, dict)
    return entry.after


LOGIN = "/django-admin/login/"
VERIFY = "/django-admin/2fa/"
RECOVERY = "/django-admin/2fa/recovery/"
ENROL = "/django-admin/2fa/enrol/"


def password_step(client: Client, email: str, **extra: str) -> Any:
    return client.post(LOGIN, {"username": email, "password": TEST_PASSWORD, **extra})


def actions() -> list[str]:
    return list(AuditEntry.objects.order_by("id").values_list("action", flat=True))


def is_verified_console(client: Client) -> bool:
    return client.get("/django-admin/").status_code == 200


def shift_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    moved = timeutils.now() + timedelta(seconds=seconds)
    monkeypatch.setattr(timeutils, "now", lambda: moved)


# --- enrolment ---------------------------------------------------------------------------------------------


def test_first_login_enrols_a_totp_device_and_shows_ten_recovery_codes_once() -> None:
    user = StaffUserFactory.create(email="new@x.uz", is_staff=True)
    client = Client()
    first = password_step(client, "new@x.uz")
    assert first.status_code == 302 and first["Location"] == ENROL
    assert SESSION_KEY not in client.session  # the password alone logs nobody in
    assert not is_verified_console(client)

    page = client.get(ENROL)
    assert page.status_code == 200
    content = page.content.decode()
    assert "<svg" in content and 'class="ops-qr"' in content  # inline QR, no image request
    assert "<img" not in content.split("ops-qr-frame")[1].split("</div>")[0]
    device = TOTPDevice.objects.get(user=user)
    assert not device.confirmed and (device.step, device.tolerance, device.digits) == (30, 1, 6)
    assert twofactor.manual_key(device) in content
    assert "no-store" in page["Cache-Control"]
    # reloading keeps the same pending secret
    client.get(ENROL)
    assert TOTPDevice.objects.get(user=user).pk == device.pk

    wrong = client.post(ENROL, {"code": "000000" if totp_code(device) != "000000" else "111111"})
    assert wrong.status_code == 200 and "Kod noto‘g‘ri" in wrong.content.decode()
    assert not is_verified_console(client)

    done = client.post(ENROL, {"code": totp_code(device)})
    assert done.status_code == 200
    codes = re.findall(r"<code>([a-z2-7]{5}-[a-z2-7]{5})</code>", done.content.decode())
    assert len(codes) == 10 and len(set(codes)) == 10
    device.refresh_from_db()
    assert device.confirmed
    # only keyed hashes are stored
    stored = set(StaticToken.objects.filter(device__user=user).values_list("token", flat=True))
    assert len(stored) == 10
    assert not {code.replace("-", "") for code in codes} & stored
    assert {twofactor.recovery_hash(code) for code in codes} == stored
    assert is_verified_console(client)
    assert client.session[DEVICE_ID_SESSION_KEY] == device.persistent_id
    # shown once: the enrolment page is gone for a user with a device, and the index never repeats them
    assert client.get(ENROL).status_code == 302
    assert codes[0] not in client.get("/django-admin/").content.decode()
    assert actions()[-3:] == ["console.otp_failed", "console.2fa_enrolled", "console.login_succeeded"]
    assert AuditEntry.objects.filter(action="console.password_verified", entity_id=str(user.pk)).exists()


def test_enrolment_restarted_by_another_session_replaces_the_pending_secret() -> None:
    StaffUserFactory.create(email="new@x.uz", is_staff=True)
    first, second = Client(), Client()
    password_step(first, "new@x.uz")
    first.get(ENROL)
    stale = TOTPDevice.objects.get(user__email="new@x.uz")
    password_step(second, "new@x.uz")
    second.get(ENROL)
    fresh = TOTPDevice.objects.get(user__email="new@x.uz")
    assert fresh.pk != stale.pk
    # the first browser's secret no longer exists: its code cannot confirm anything
    answer = first.post(ENROL, {"code": totp_code_for_key(stale.key)})
    assert answer.status_code == 200 and not TOTPDevice.objects.filter(confirmed=True).exists()


def test_a_user_with_a_device_is_never_offered_enrolment() -> None:
    ops_staff("ops@x.uz")
    client = Client()
    assert password_step(client, "ops@x.uz")["Location"] == VERIFY
    assert client.get(ENROL)["Location"] == VERIFY


# --- TOTP code step ----------------------------------------------------------------------------------------


def test_code_step_verifies_the_session_and_cycles_its_key() -> None:
    user = ops_staff("ops@x.uz")
    client = Client()
    client.get(LOGIN)
    before = client.cookies.get("sessionid")
    password_step(client, "ops@x.uz")
    pending_key = client.session.session_key
    assert before is None or before.value != pending_key
    page = client.get(VERIFY)
    assert page.status_code == 200 and "ops@x.uz" in page.content.decode()
    answer = client.post(VERIFY, {"code": totp_code(TOTPDevice.objects.get(user=user))})
    assert answer.status_code == 302 and answer["Location"] == "/django-admin/"
    assert client.session.session_key != pending_key
    assert client.session[SESSION_KEY] == str(user.pk)
    assert state.PENDING not in client.session
    assert is_verified_console(client)
    entry = AuditEntry.objects.filter(action="console.login_succeeded").get()
    assert after(entry)["method"] == "totp" and entry.actor_id == user.pk


def test_the_same_code_is_refused_the_second_time() -> None:
    user = ops_staff("ops@x.uz")
    device = TOTPDevice.objects.get(user=user)
    code = totp_code(device)
    first = Client()
    password_step(first, "ops@x.uz")
    assert first.post(VERIFY, {"code": code}).status_code == 302
    replay = Client()
    password_step(replay, "ops@x.uz")
    answer = replay.post(VERIFY, {"code": code})
    assert answer.status_code == 200 and "Kod noto‘g‘ri" in answer.content.decode()
    assert not is_verified_console(replay)
    device.refresh_from_db()
    assert device.last_t >= 0  # django-otp keeps the last verified step: only later steps verify


def test_next_is_kept_when_safe_and_dropped_otherwise() -> None:
    user = ops_staff("ops@x.uz")
    target = reverse("admin:core_auditentry_changelist")
    client = Client()
    password_step(client, "ops@x.uz", next=target)
    assert client.post(VERIFY, {"code": totp_code(TOTPDevice.objects.get(user=user))})["Location"] == target
    other = Client()
    password_step(other, "ops@x.uz", next="https://evil.example/")
    assert other.post(VERIFY, {"code": totp_code(TOTPDevice.objects.get(user=user))})["Location"] == (
        "/django-admin/"
    )


def test_wrong_codes_lock_the_code_step_on_the_login_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    user = ops_staff("ops@x.uz")
    device = TOTPDevice.objects.get(user=user)
    good = totp_code(device)
    wrong = "000000" if good != "000000" else "111111"
    client = Client()
    password_step(client, "ops@x.uz")
    for _ in range(5):
        assert client.post(VERIFY, {"code": wrong}).status_code == 200
    locked = client.post(VERIFY, {"code": good})
    assert locked.status_code == 429 and locked["Retry-After"] == "60"
    assert "Juda ko‘p urinish. 60 soniyadan so‘ng" in locked.content.decode()
    assert not is_verified_console(client)
    assert AuditEntry.objects.filter(action="console.otp_failed").count() == 5
    assert AuditEntry.objects.filter(action="console.otp_locked").exists()
    # A minute later the second lock lasts twice as long (1, 2, 4, 8, then 15 minutes).
    shift_clock(monkeypatch, 61)
    hashed = email_hash(user.email)
    cache.delete(f"auth:attempt:django_admin_otp:{hashed}")
    cache.delete(f"auth:fail:django_admin_otp:{hashed}")
    password_step(client, "ops@x.uz")
    for _ in range(5):
        assert client.post(VERIFY, {"code": wrong}).status_code == 200
    again = client.post(VERIFY, {"code": good})
    assert again.status_code == 429 and again["Retry-After"] == "120"


def test_format_errors_count_as_failed_attempts() -> None:
    ops_staff("ops@x.uz")
    client = Client()
    password_step(client, "ops@x.uz")
    answer = client.post(VERIFY, {"code": "12ab"})
    assert answer.status_code == 200 and "Kod 6 ta raqamdan iborat." in answer.content.decode()
    assert AuditEntry.objects.filter(action="console.otp_failed").count() == 1


def test_a_pending_sign_in_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    user = ops_staff("ops@x.uz")
    client = Client()
    password_step(client, "ops@x.uz")
    shift_clock(monkeypatch, state.PENDING_SECONDS + 1)
    answer = client.post(VERIFY, {"code": totp_code(TOTPDevice.objects.get(user=user))}, follow=True)
    assert answer.redirect_chain[0] == (LOGIN, 302)
    assert "Kirish muddati tugadi" in answer.content.decode()
    assert not is_verified_console(client)


def test_a_password_change_ends_the_pending_sign_in() -> None:
    user = ops_staff("ops@x.uz")
    client = Client()
    password_step(client, "ops@x.uz")
    user.set_password("Another!Harbor-Long99")
    user.save()
    assert client.get(VERIFY)["Location"] == LOGIN


def test_steps_without_a_pending_sign_in_go_to_the_login_page() -> None:
    for path in (VERIFY, RECOVERY, ENROL):
        assert Client().get(path)["Location"] == LOGIN


def test_cancel_drops_the_pending_sign_in() -> None:
    ops_staff("ops@x.uz")
    client = Client()
    password_step(client, "ops@x.uz")
    assert client.post("/django-admin/2fa/cancel/")["Location"] == LOGIN
    assert client.get(VERIFY)["Location"] == LOGIN
    assert Client().get("/django-admin/2fa/cancel/").status_code == 405


def test_non_staff_and_inactive_users_never_reach_the_second_step() -> None:
    StaffUserFactory.create(email="panel@x.uz", is_staff=False)
    StaffUserFactory.create(email="gone@x.uz", is_staff=True, is_active=False)
    for email in ("panel@x.uz", "gone@x.uz"):
        client = Client()
        answer = password_step(client, email)
        assert answer.status_code == 200 and state.PENDING not in client.session
    assert AuditEntry.objects.filter(action="console.login_failed").count() == 2


def test_password_lockout_renders_the_login_page_and_is_audited() -> None:
    client = Client()
    for _ in range(5):
        client.post(LOGIN, {"username": "who@x.uz", "password": "wrong"})
    locked = client.post(LOGIN, {"username": "who@x.uz", "password": "wrong"})
    assert locked.status_code == 429
    assert "ops-lockout" in locked.content.decode()
    assert after(AuditEntry.objects.filter(action="console.login_locked").get())["email"] == "who@x.uz"


@override_settings(OPS_REQUIRE_2FA=False)
def test_without_the_2fa_requirement_the_password_logs_in() -> None:
    StaffUserFactory.create(email="ops@x.uz", is_staff=True)
    client = Client()
    answer = password_step(client, "ops@x.uz")
    assert answer.status_code == 302 and answer["Location"] == "/django-admin/"
    assert is_verified_console(client)
    assert after(AuditEntry.objects.get(action="console.login_succeeded"))["method"] == "password"


# --- recovery codes ----------------------------------------------------------------------------------------


def test_a_recovery_code_works_once_and_is_audited() -> None:
    user = ops_staff("ops@x.uz")
    codes = twofactor.issue_recovery_codes(user)
    client = Client()
    password_step(client, "ops@x.uz")
    assert client.get(RECOVERY).status_code == 200
    answer = client.post(RECOVERY, {"code": codes[0].upper()})
    assert answer.status_code == 302 and is_verified_console(client)
    used = AuditEntry.objects.get(action="console.recovery_code_used")
    assert after(used)["remaining"] == 9 and codes[0] not in str(after(used))
    assert client.session[DEVICE_ID_SESSION_KEY] == StaticDevice.objects.get(user=user).persistent_id
    again = Client()
    password_step(again, "ops@x.uz")
    refused = again.post(RECOVERY, {"code": codes[0]})
    assert refused.status_code == 200 and "Zaxira kod noto‘g‘ri" in refused.content.decode()
    assert not is_verified_console(again)


def test_a_stored_hash_is_not_a_recovery_code() -> None:
    user = ops_staff("ops@x.uz")
    twofactor.issue_recovery_codes(user)
    stored = StaticToken.objects.filter(device__user=user).values_list("token", flat=True).first()
    assert stored is not None
    client = Client()
    password_step(client, "ops@x.uz")
    assert client.post(RECOVERY, {"code": stored}).status_code == 200
    assert twofactor.use_recovery_code(user, stored) is None
    assert StaticToken.objects.filter(token=stored).exists()


def test_recovery_codes_survive_a_secret_key_rotation(settings: Any) -> None:
    user = ops_staff("ops@x.uz")
    codes = twofactor.issue_recovery_codes(user)
    old = settings.SECRET_KEY
    settings.SECRET_KEY = "rotated-" + old
    settings.SECRET_KEY_FALLBACKS = [old]
    assert twofactor.use_recovery_code(user, codes[1]) is not None


def test_few_recovery_codes_left_warns() -> None:
    user = ops_staff("ops@x.uz")
    codes = twofactor.issue_recovery_codes(user)
    for code in codes[:7]:
        assert twofactor.use_recovery_code(user, code) is not None
    client = Client()
    password_step(client, "ops@x.uz")
    answer = client.post(RECOVERY, {"code": codes[7]}, follow=True)
    assert "Zaxira kodlardan 2 tasi qoldi" in answer.content.decode()


# --- every console view needs the verified session ---------------------------------------------------------


def admin_urls() -> list[str]:
    urls = [reverse("admin:index"), reverse("admin:jsi18n"), reverse("admin:search")]
    for model in admin.site._registry:
        meta = model._meta
        urls.append(reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist"))
        urls.append(reverse("admin:app_list", args=[meta.app_label]))
    return sorted(set(urls))


@pytest.mark.parametrize("url", admin_urls())
def test_an_unverified_session_reaches_no_console_page(url: str) -> None:
    client = Client()
    client.force_login(ops_staff("ops@x.uz", is_superuser=True))  # password-level session, no 2FA
    answer = client.get(url)
    assert answer.status_code == 302, url
    assert answer["Location"].startswith(LOGIN)


def test_a_session_verified_by_a_deleted_device_is_unverified() -> None:
    user = ops_staff("ops@x.uz")
    client = Client()
    login_through_forms(client, "ops@x.uz")
    assert is_verified_console(client)
    twofactor.reset_devices(user)
    assert client.get("/django-admin/").status_code == 302


def test_logout_ends_the_session_and_is_audited() -> None:
    ops_staff("ops@x.uz")
    client = Client()
    login_through_forms(client, "ops@x.uz")
    assert client.get("/django-admin/logout/").status_code == 405
    answer = client.post("/django-admin/logout/", follow=True)
    assert answer.redirect_chain[-1][0] == LOGIN
    assert "Siz konsoldan chiqdingiz." in answer.content.decode()
    assert SESSION_KEY not in client.session
    assert AuditEntry.objects.filter(action="console.logout").count() == 1


def test_the_console_logout_link_posts_to_the_audited_view(ops_client: Client) -> None:
    page = ops_client.get("/django-admin/").content.decode()
    assert 'action="/django-admin/logout/"' in page


def test_the_known_key_helper_matches_django_otp() -> None:
    user = ops_staff("ops@x.uz")
    device = TOTPDevice.objects.get(user=user)
    assert device.key == KNOWN_TOTP_KEY
    assert totp_code(device) in {totp_code_for_key(KNOWN_TOTP_KEY, step_offset=offset) for offset in (0, 1)}
    assert StaffUser.objects.get(email="ops@x.uz").is_staff
