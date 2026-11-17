"""The admin panel's optional TOTP MFA (feat/admin-ux, ``glowderma.accounts.mfa``; admin contract §2.1).

Modelled on ``test_console_2fa.py``: codes come from ``ops_helpers.totp_code`` on fixed keys. The panel and
the console use different keys here, so a code of one device never happens to be the other's.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.test import APIClient

from glowderma.accounts import mfa, services, tokens
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.console import twofactor
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, AuthSessionFactory, StaffUserFactory
from glowderma.accounts.tests.ops_helpers import KNOWN_TOTP_KEY, ops_staff, totp_code, totp_code_for_key
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry, OutboxMessage

pytestmark = pytest.mark.django_db
AUTH = "/api/v1/admin/auth/"
MFA = AUTH + "mfa/"
VERIFY = MFA + "verify/"
PANEL_KEY = KNOWN_TOTP_KEY
CONSOLE_KEY = "4142434445464748494a4b4c4d4e4f5051525354"
LOGIN_KEYS = {"access", "access_expires_at", "refresh", "refresh_expires_at", "user"}
ALERTS = override_settings(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook")


# --- helpers -----------------------------------------------------------------------------------------------


def panel_device(user: StaffUser, key: str = PANEL_KEY, *, confirmed: bool = True) -> TOTPDevice:
    return TOTPDevice.objects.create(
        user=user,
        name=twofactor.PANEL_DEVICE_NAME,
        key=key,
        confirmed=confirmed,
        step=30,
        tolerance=1,
        digits=6,
    )


def enable(user: StaffUser) -> tuple[TOTPDevice, list[str]]:
    """A confirmed panel device on ``PANEL_KEY`` plus ten panel recovery codes."""
    return panel_device(user), twofactor.issue_recovery_codes(user, scope=twofactor.PANEL)


def rewind(device: TOTPDevice) -> None:
    """Time passed: the device accepts the current step again (only where a test needs a second code)."""
    TOTPDevice.objects.filter(pk=device.pk).update(last_t=-1)


def bearer(user: StaffUser) -> tuple[APIClient, AuthSession]:
    session = AuthSessionFactory.create(staff=user)
    access, _ = tokens.issue_access(session)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client, session


def login(email: str, password: str = TEST_PASSWORD, client: APIClient | None = None) -> Any:
    return (client or APIClient()).post(
        AUTH + "login/", {"email": email, "password": password}, format="json"
    )


def challenge_for(user: StaffUser) -> str:
    answer = login(user.email)
    assert answer.status_code == 200, answer.data
    assert answer.data["mfa_required"] is True
    return str(answer.data["challenge"])


def verify(challenge: str, client: APIClient | None = None, **factor: str) -> Any:
    return (client or APIClient()).post(VERIFY, {"challenge": challenge, **factor}, format="json")


def code_of(answer: Any) -> str:
    return str(answer.data["code"])


@pytest.fixture
def user() -> StaffUser:
    return StaffUserFactory.create(email="ali@x.uz")


# --- the device scopes: panel and console never see each other's rows ---------------------------------------


def test_panel_and_console_devices_are_separate_scopes(user: StaffUser) -> None:
    console = ops_staff(user.email, key=CONSOLE_KEY)
    assert twofactor.has_second_factor(console, scope=twofactor.CONSOLE)
    assert not twofactor.has_second_factor(console, scope=twofactor.PANEL)
    assert not mfa.enabled(console)
    device, panel_codes = enable(user)
    console_codes = twofactor.issue_recovery_codes(user, scope=twofactor.CONSOLE)
    assert twofactor.confirmed_totp(user, scope=twofactor.PANEL) == device
    assert twofactor.confirmed_totp(user, scope=twofactor.CONSOLE) != device
    # the panel's recovery codes did not replace the console's (and vice versa)
    assert twofactor.recovery_codes_left(user, scope=twofactor.PANEL) == 10
    assert twofactor.recovery_codes_left(user, scope=twofactor.CONSOLE) == 10
    assert twofactor.use_recovery_code(user, console_codes[0], scope=twofactor.PANEL) is None
    assert twofactor.use_recovery_code(user, panel_codes[0], scope=twofactor.CONSOLE) is None
    # a console code is no panel code and the other way round
    assert twofactor.verify_totp(user, totp_code_for_key(CONSOLE_KEY), scope=twofactor.PANEL) is None
    assert twofactor.verify_totp(user, totp_code_for_key(PANEL_KEY), scope=twofactor.CONSOLE) is None
    # resetting one scope leaves the other
    assert twofactor.reset_devices(user, scope=twofactor.PANEL) == {"totp_devices": 1, "recovery_codes": 10}
    assert twofactor.has_second_factor(user, scope=twofactor.CONSOLE)
    assert twofactor.recovery_codes_left(user, scope=twofactor.CONSOLE) == 10
    enable(user)
    twofactor.reset_devices(user, scope=twofactor.CONSOLE)
    assert mfa.enabled(user) and twofactor.recovery_codes_left(user, scope=twofactor.PANEL) == 10


def test_a_console_enrolment_keeps_the_panel_device(user: StaffUser) -> None:
    device, _ = enable(user)
    pending = twofactor.enrolment_device(user, None, scope=twofactor.CONSOLE)
    assert pending.name == twofactor.DEVICE_NAME
    done = twofactor.confirm_enrolment(user, pending.pk, totp_code(pending), scope=twofactor.CONSOLE)
    assert done is not None
    assert TOTPDevice.objects.filter(pk=device.pk, confirmed=True).exists()
    assert twofactor.recovery_codes_left(user, scope=twofactor.PANEL) == 10
    # and a panel enrolment keeps the console device
    twofactor.reset_devices(user, scope=twofactor.PANEL)
    panel_pending = twofactor.enrolment_device(user, None, scope=twofactor.PANEL)
    assert twofactor.CONSOLE.totp_devices(user).filter(confirmed=False).count() == 0
    assert twofactor.confirm_enrolment(
        user, panel_pending.pk, totp_code(panel_pending), scope=twofactor.PANEL
    )
    assert TOTPDevice.objects.filter(pk=done.device.pk, confirmed=True).exists()


def test_a_panel_device_does_not_open_the_console() -> None:
    staff = StaffUserFactory.create(email="ops@x.uz", is_staff=True)
    panel_device(staff)
    client = Client()
    answer = client.post("/django-admin/login/", {"username": staff.email, "password": TEST_PASSWORD})
    # no console device: the console sends the person to its own (window-gated) enrolment, not the code step
    assert answer.status_code == 302 and answer["Location"] == "/django-admin/2fa/enrol/"


def test_the_panel_uri_names_the_admin_issuer_and_the_console_keeps_its_own(user: StaffUser) -> None:
    device = panel_device(user, confirmed=False)
    uri = twofactor.otpauth_uri(device, scope=twofactor.PANEL)
    parsed = urlparse(uri)
    assert parsed.scheme == "otpauth" and parsed.netloc == "totp"
    assert unquote(parsed.path) == "/Glow Derma Admin:ali@x.uz"
    query = parse_qs(parsed.query)
    assert query["issuer"] == ["Glow Derma Admin"] and query["digits"] == ["6"] and query["period"] == ["30"]
    assert query["secret"] == [twofactor.manual_key(device).replace(" ", "")]
    console = ops_staff("ops@x.uz", key=CONSOLE_KEY)
    console_device = twofactor.confirmed_totp(console, scope=twofactor.CONSOLE)
    assert "issuer=Glow%20Derma%20Ops" in twofactor.otpauth_uri(console_device, scope=twofactor.CONSOLE)


# --- status, setup and confirm -----------------------------------------------------------------------------


def test_status_is_off_by_default(user: StaffUser) -> None:
    client, _ = bearer(user)
    assert client.get(MFA).data == {"enabled": False, "recovery_codes_left": 0}
    assert client.get(AUTH + "me/").data["mfa_enabled"] is False


def test_setup_needs_the_current_password(user: StaffUser) -> None:
    client, _ = bearer(user)
    answer = client.post(MFA + "setup/", {"current_password": "wrong"}, format="json")
    assert answer.status_code == 400
    assert answer.data["errors"][0]["field"] == "current_password"
    assert answer.data["errors"][0]["code"] == "password_incorrect"
    assert not TOTPDevice.objects.filter(user=user).exists()
    assert client.post(MFA + "setup/", {}, format="json").status_code == 400


def test_setup_password_guesses_share_the_change_password_lockout(user: StaffUser) -> None:
    client, _ = bearer(user)
    for _ in range(5):
        assert client.post(MFA + "setup/", {"current_password": "wrong"}, format="json").status_code == 400
    locked = client.post(MFA + "setup/", {"current_password": TEST_PASSWORD}, format="json")
    assert locked.status_code == 429
    change = client.post(
        AUTH + "change-password/",
        {"current_password": TEST_PASSWORD, "new_password": "Another!Pass-99"},
        format="json",
    )
    assert change.status_code == 429


def test_setup_and_confirm_enable_mfa(user: StaffUser) -> None:
    client, session = bearer(user)
    first = client.post(MFA + "setup/", {"current_password": TEST_PASSWORD}, format="json")
    assert first.status_code == 200, first.data
    assert set(first.data) == {"device_id", "otpauth_uri", "manual_key"}
    assert first.data["otpauth_uri"].startswith("otpauth://totp/Glow%20Derma%20Admin%3Aali%40x.uz?")
    # a second setup replaces the pending device
    second = client.post(MFA + "setup/", {"current_password": TEST_PASSWORD}, format="json")
    assert second.data["device_id"] != first.data["device_id"]
    assert list(TOTPDevice.objects.filter(user=user).values_list("pk", "name", "confirmed")) == [
        (second.data["device_id"], twofactor.PANEL_DEVICE_NAME, False)
    ]
    device = TOTPDevice.objects.get(pk=second.data["device_id"])
    assert twofactor.manual_key(device) == second.data["manual_key"]
    answer = client.post(MFA + "confirm/", {"device_id": device.pk, "code": totp_code(device)}, format="json")
    assert answer.status_code == 200, answer.data
    codes = answer.data["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    stored = set(StaticToken.objects.filter(device__user=user).values_list("token", flat=True))
    assert not stored & {code.replace("-", "") for code in codes}  # only keyed hashes are kept
    assert client.get(MFA).data == {"enabled": True, "recovery_codes_left": 10}
    me = client.get(AUTH + "me/").data
    assert me["mfa_enabled"] is True
    session.refresh_from_db()
    assert session.mfa_verified_at is not None
    entry = AuditEntry.objects.get(action="auth.mfa_enabled")
    assert entry.after == {"recovery_codes": 10} and entry.actor_id == user.pk
    assert StaticDevice.objects.get(user=user).name == twofactor.PANEL_RECOVERY_DEVICE_NAME
    # already on
    assert client.post(MFA + "setup/", {"current_password": TEST_PASSWORD}, format="json").status_code == 409
    again = client.post(MFA + "confirm/", {"device_id": device.pk, "code": "123456"}, format="json")
    assert again.status_code == 409


def test_confirm_refuses_a_wrong_code_and_a_foreign_device(user: StaffUser) -> None:
    client, _ = bearer(user)
    setup = client.post(MFA + "setup/", {"current_password": TEST_PASSWORD}, format="json").data
    device = TOTPDevice.objects.get(pk=setup["device_id"])
    wrong = "000000" if totp_code(device) != "000000" else "111111"
    answer = client.post(MFA + "confirm/", {"device_id": device.pk, "code": wrong}, format="json")
    assert answer.status_code == 400 and code_of(answer) == "mfa_code_invalid"
    assert AuditEntry.objects.get(action="auth.mfa_failed").after == {"factor": "totp", "context": "setup"}
    # another person's pending device, or the caller's own console device, cannot be confirmed here
    other = StaffUserFactory.create(email="other@x.uz")
    foreign = panel_device(other, confirmed=False)
    answer = client.post(
        MFA + "confirm/", {"device_id": foreign.pk, "code": totp_code(foreign)}, format="json"
    )
    assert code_of(answer) == "mfa_code_invalid"
    console = TOTPDevice.objects.create(
        user=user, name=twofactor.DEVICE_NAME, key=CONSOLE_KEY, confirmed=False
    )
    answer = client.post(
        MFA + "confirm/", {"device_id": console.pk, "code": totp_code(console)}, format="json"
    )
    assert code_of(answer) == "mfa_code_invalid"
    console.refresh_from_db()
    assert not console.confirmed
    assert not mfa.enabled(user) and not mfa.enabled(other)


def test_mfa_routes_wait_for_a_pending_password_change(user: StaffUser) -> None:
    StaffUser.objects.filter(pk=user.pk).update(must_change_password=True)
    client, _ = bearer(user)
    for method, path in (("get", MFA), ("post", MFA + "setup/"), ("post", MFA + "disable/")):
        answer = getattr(client, method)(path, {}, format="json")
        assert answer.status_code == 403 and code_of(answer) == "password_change_required"


def test_mfa_routes_need_a_bearer() -> None:
    for path in ("", "setup/", "confirm/", "disable/", "recovery-codes/"):
        assert APIClient().post(MFA + path, {}, format="json").status_code == 401


# --- the sign-in challenge ---------------------------------------------------------------------------------


def test_password_login_without_mfa_is_unchanged(user: StaffUser) -> None:
    ops_staff(user.email, key=CONSOLE_KEY)  # a console device is not the panel's MFA
    answer = login(user.email)
    assert set(answer.data) == LOGIN_KEYS
    assert answer.data["user"]["mfa_enabled"] is False


def test_password_login_with_mfa_answers_a_challenge_then_verify_opens_the_session(user: StaffUser) -> None:
    device, _ = enable(user)
    answer = login(user.email)
    assert answer.status_code == 200
    assert set(answer.data) == {"mfa_required", "challenge", "challenge_expires_at"}
    assert answer.data["challenge_expires_at"].endswith("+05:00")
    assert not AuthSession.objects.filter(staff=user).exists()
    assert not AuditEntry.objects.filter(action="auth.login_succeeded").exists()
    assert AuditEntry.objects.get(action="auth.mfa_challenged").after == {
        "email": user.email,
        "method": "password",
    }
    done = verify(answer.data["challenge"], code=totp_code(device))
    assert done.status_code == 200, done.data
    assert set(done.data) == LOGIN_KEYS
    assert done.data["user"]["auth_method"] == "password" and done.data["user"]["mfa_enabled"] is True
    session = AuthSession.objects.get(staff=user)
    assert session.auth_method == "password" and session.mfa_verified_at is not None
    assert AuditEntry.objects.get(action="auth.mfa_succeeded").after == {
        "factor": "totp",
        "method": "password",
    }
    assert AuditEntry.objects.get(action="auth.login_succeeded").after == {
        "email": user.email,
        "method": "password",
        "mfa": "totp",
    }
    # the tokens work
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {done.data['access']}")
    assert client.get(AUTH + "me/").status_code == 200
    # single use
    rewind(device)
    reused = verify(answer.data["challenge"], code=totp_code(device))
    assert reused.status_code == 400 and code_of(reused) == "mfa_challenge_invalid"
    assert AuthSession.objects.filter(staff=user).count() == 1


def test_a_used_code_is_refused_on_a_new_challenge(user: StaffUser) -> None:
    device, _ = enable(user)
    code = totp_code(device)
    assert verify(challenge_for(user), code=code).status_code == 200
    replay = verify(challenge_for(user), code=code)
    assert replay.status_code == 400 and code_of(replay) == "mfa_code_invalid"


def test_a_recovery_code_signs_in_once(user: StaffUser) -> None:
    _, codes = enable(user)
    done = verify(challenge_for(user), recovery_code=codes[0].upper().replace("-", " - "))
    assert done.status_code == 200, done.data
    assert AuditEntry.objects.get(action="auth.mfa_recovery_used").after == {"remaining": 9}
    assert AuditEntry.objects.get(action="auth.login_succeeded").after["mfa"] == "recovery"  # type: ignore[index]
    again = verify(challenge_for(user), recovery_code=codes[0])
    assert code_of(again) == "mfa_code_invalid"
    assert twofactor.recovery_codes_left(user, scope=twofactor.PANEL) == 9


def test_a_console_code_or_recovery_code_does_not_pass_the_panel_step(user: StaffUser) -> None:
    enable(user)
    console = ops_staff(user.email, key=CONSOLE_KEY)
    console_codes = twofactor.issue_recovery_codes(console, scope=twofactor.CONSOLE)
    challenge = challenge_for(user)
    assert code_of(verify(challenge, code=totp_code_for_key(CONSOLE_KEY))) == "mfa_code_invalid"
    assert code_of(verify(challenge, recovery_code=console_codes[0])) == "mfa_code_invalid"
    assert twofactor.recovery_codes_left(console, scope=twofactor.CONSOLE) == 10


def test_a_wrong_code_is_audited_and_keeps_the_challenge(user: StaffUser) -> None:
    device, _ = enable(user)
    challenge = challenge_for(user)
    wrong = "000000" if totp_code(device) != "000000" else "111111"
    answer = verify(challenge, code=wrong)
    assert answer.status_code == 400 and code_of(answer) == "mfa_code_invalid"
    assert AuditEntry.objects.get(action="auth.mfa_failed").after == {"factor": "totp", "context": "login"}
    assert verify(challenge, code=totp_code(device)).status_code == 200


def test_a_challenge_allows_five_attempts(user: StaffUser) -> None:
    device, _ = enable(user)
    challenge = challenge_for(user)
    guard = LoginGuard()
    for _ in range(mfa.CHALLENGE_MAX_ATTEMPTS):
        assert code_of(verify(challenge, code="000001")) == "mfa_code_invalid"
        guard.unlock(mfa.GUARD_SCOPE, email_hash(user.email))  # isolate the challenge's own cap
    exhausted = verify(challenge, code=totp_code(device))
    assert exhausted.status_code == 400 and code_of(exhausted) == "mfa_challenge_invalid"
    assert not AuthSession.objects.filter(staff=user).exists()


@ALERTS
def test_five_wrong_codes_lock_the_account_audit_and_alert(user: StaffUser) -> None:
    device, _ = enable(user)
    for _ in range(5):
        assert code_of(verify(challenge_for(user), code="000001")) == "mfa_code_invalid"
    locked = verify(challenge_for(user), code=totp_code(device))
    assert locked.status_code == 429 and int(locked["Retry-After"]) > 0
    entry = AuditEntry.objects.get(action="auth.mfa_locked")
    assert entry.entity_id == str(user.pk) and entry.after["wait"] > 0  # type: ignore[index]
    alert = OutboxMessage.objects.get(topic="alert.send", payload__event="auth.mfa_locked")
    assert alert.payload["fields"]["staff_id"] == user.pk
    assert not AuthSession.objects.filter(staff=user).exists()
    # the lockout also covers the settings' code checks
    client, _ = bearer(user)
    answer = client.post(
        MFA + "recovery-codes/", {"current_password": TEST_PASSWORD, "code": totp_code(device)}, format="json"
    )
    assert answer.status_code == 429


def a_minute_passes(user: StaffUser) -> None:
    """The ``panel_otp`` minute window ends (its counters expire); the failure streak stays."""
    hashed = email_hash(user.email)
    cache.delete_many([f"auth:fail:{mfa.GUARD_SCOPE}:{hashed}", f"auth:attempt:{mfa.GUARD_SCOPE}:{hashed}"])


def wrong_codes_under_the_minute_lock(user: StaffUser, count: int) -> None:
    """``count`` wrong codes, four a minute, each on a fresh challenge (the challenge's cap renewed by
    signing in again)."""
    for n in range(count):
        if n and n % 4 == 0:
            a_minute_passes(user)
        assert code_of(verify(challenge_for(user), code="000001")) == "mfa_code_invalid"


@ALERTS
def test_wrong_codes_under_five_a_minute_still_lock_audit_and_alert(user: StaffUser) -> None:
    device, _ = enable(user)
    wrong_codes_under_the_minute_lock(user, mfa.STREAK_LIMIT - 1)
    assert not AuditEntry.objects.filter(action="auth.mfa_locked").exists()
    a_minute_passes(user)
    assert code_of(verify(challenge_for(user), code="000001")) == "mfa_code_invalid"
    # the tenth wrong code in a row locks for 15 minutes, audited and alerted at once
    entry = AuditEntry.objects.get(action="auth.mfa_locked")
    assert entry.entity_id == str(user.pk) and entry.after == {"wait": 900, "failures": mfa.STREAK_LIMIT}
    alert = OutboxMessage.objects.get(topic="alert.send", payload__event="auth.mfa_locked")
    assert alert.payload["fields"] == {"staff_id": user.pk, "wait": 900, "failures": mfa.STREAK_LIMIT}
    assert AuditEntry.objects.filter(action="auth.mfa_failed").count() == mfa.STREAK_LIMIT
    a_minute_passes(user)
    locked = verify(challenge_for(user), code=totp_code(device))
    assert locked.status_code == 429 and 0 < int(locked["Retry-After"]) <= 900
    assert not AuthSession.objects.filter(staff=user).exists()


def test_a_correct_code_clears_the_wrong_code_streak(user: StaffUser) -> None:
    device, _ = enable(user)
    wrong_codes_under_the_minute_lock(user, mfa.STREAK_LIMIT - 1)
    a_minute_passes(user)
    assert verify(challenge_for(user), code=totp_code(device)).status_code == 200
    a_minute_passes(user)
    wrong_codes_under_the_minute_lock(user, mfa.STREAK_LIMIT - 1)
    assert not AuditEntry.objects.filter(action="auth.mfa_locked").exists()
    assert cache.get(f"auth:lock:{mfa.GUARD_SCOPE}:{email_hash(user.email)}") is None


def test_the_challenge_expires(user: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    device, _ = enable(user)
    challenge = challenge_for(user)
    later = timeutils.now() + timedelta(seconds=mfa.CHALLENGE_TTL_SECONDS + 1)
    monkeypatch.setattr(timeutils, "now", lambda: later)
    assert code_of(verify(challenge, code=totp_code(device))) == "mfa_challenge_invalid"


@pytest.mark.parametrize("change", ["password", "deactivated", "mfa_reset", "mfa_disabled"])
def test_the_challenge_dies_with_the_account_state(user: StaffUser, change: str) -> None:
    enable(user)
    challenge = challenge_for(user)
    if change == "password":
        user.set_password("Brand!New-Secret-77")
        user.save(update_fields=["password"])
    elif change == "deactivated":
        StaffUser.objects.filter(pk=user.pk).update(is_active=False)
    elif change == "mfa_reset":
        manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
        services.reset_staff_mfa(user, actor=manager)
    else:
        twofactor.reset_devices(user, scope=twofactor.PANEL)
    answer = verify(challenge, code=totp_code_for_key(PANEL_KEY))
    assert answer.status_code == 400 and code_of(answer) == "mfa_challenge_invalid"
    assert not AuthSession.objects.filter(staff=user, revoked_at__isnull=True).exists()


def test_verify_validates_its_body(user: StaffUser) -> None:
    _, codes = enable(user)
    challenge = challenge_for(user)
    both = verify(challenge, code="123456", recovery_code=codes[0])
    assert both.status_code == 400 and code_of(both) == "validation_error"
    neither = verify(challenge)
    assert neither.status_code == 400 and neither.data["errors"][0]["code"] == "required"
    unknown = verify(secrets.token_urlsafe(32), code="123456")
    assert code_of(unknown) == "mfa_challenge_invalid"
    too_long = verify("x" * 200, code="123456")
    assert too_long.status_code == 400 and code_of(too_long) == "validation_error"
    malformed = verify(challenge, code="12ab56")
    assert code_of(malformed) == "mfa_code_invalid"


def test_verify_is_throttled_per_ip(user: StaffUser) -> None:
    with override_settings(THROTTLE_RATES={"mfa_verify": "2/min"}):
        for _ in range(2):
            assert verify("nope", code="123456").status_code == 400
        assert verify("nope", code="123456").status_code == 429


def test_the_challenge_is_claimed_once(user: StaffUser) -> None:
    enable(user)
    issued = mfa.issue_challenge(user, "password")
    pending = mfa.open_challenge(issued.raw)
    assert pending is not None and mfa.challenge_matches(pending, user)
    assert mfa.claim_challenge(pending) is True
    assert mfa.claim_challenge(pending) is False
    assert mfa.open_challenge(issued.raw) is None


def test_no_secret_reaches_the_audit_log(user: StaffUser) -> None:
    device, codes = enable(user)
    challenge = challenge_for(user)
    code = totp_code(device)
    verify(challenge, code="000001")
    verify(challenge, code=code)
    verify(challenge_for(user), recovery_code=codes[1])
    text = " ".join(str(row) for row in AuditEntry.objects.values_list("after", "before", "reason"))
    for secret in (challenge, code, codes[1], codes[1].replace("-", ""), device.key, TEST_PASSWORD):
        assert secret not in text


# --- OAuth sign-in -----------------------------------------------------------------------------------------


def oauth(code: str, nonce: str) -> Any:
    body = {
        "code": code,
        "code_verifier": secrets.token_urlsafe(48),
        "redirect_uri": "http://localhost:3000/admin/auth/callback",
        "nonce": nonce,
    }
    return APIClient().post(AUTH + "oauth/google/", body, format="json")


def test_oauth_login_with_mfa_answers_a_challenge(user: StaffUser) -> None:
    device, _ = enable(user)
    identity = SocialIdentity.objects.create(
        staff=user, provider="google", subject="42", email_at_link=user.email
    )
    answer = oauth(f"fake:42:{user.email}", "nonce-42")
    assert answer.status_code == 200, answer.data
    assert answer.data["mfa_required"] is True
    assert not AuthSession.objects.filter(staff=user).exists()
    identity.refresh_from_db()
    assert identity.last_login_at is None
    done = verify(answer.data["challenge"], code=totp_code(device))
    assert done.status_code == 200, done.data
    assert done.data["user"]["auth_method"] == "google"
    session = AuthSession.objects.get(staff=user)
    assert session.auth_method == "google" and session.mfa_verified_at is not None
    assert SocialIdentity.objects.get(pk=identity.pk).last_login_at is not None
    assert AuditEntry.objects.get(action="auth.login_succeeded").after == {
        "email": user.email,
        "method": "google",
        "mfa": "totp",
    }


def test_an_unlink_since_the_oauth_step_kills_the_challenge(user: StaffUser) -> None:
    enable(user)
    identity = SocialIdentity.objects.create(
        staff=user, provider="google", subject="43", email_at_link=user.email
    )
    answer = oauth(f"fake:43:{user.email}", "nonce-43")
    assert answer.data["mfa_required"] is True
    identity.delete()
    done = verify(answer.data["challenge"], code=totp_code_for_key(PANEL_KEY))
    assert done.status_code == 400 and code_of(done) == "mfa_challenge_invalid"
    assert not AuthSession.objects.filter(staff=user).exists()
    assert mfa.open_challenge(answer.data["challenge"]) is None


@override_settings(STAFF_GOOGLE_AUTOLINK=True)
def test_an_automatic_google_link_is_kept_when_mfa_stops_the_sign_in() -> None:
    staff = StaffUserFactory.create(email="ali@gmail.com")
    enable(staff)
    answer = oauth("fake:77:ali@gmail.com", "nonce-77")
    assert answer.status_code == 200 and answer.data["mfa_required"] is True
    assert SocialIdentity.objects.filter(staff=staff, provider="google", subject="77").exists()
    assert AuditEntry.objects.filter(action="auth.social_linked").count() == 1


def test_social_login_service_raises_instead_of_opening_a_session(user: StaffUser) -> None:
    from glowderma.integrations.oauth.ports import SocialClaims

    enable(user)
    SocialIdentity.objects.create(staff=user, provider="google", subject="9", email_at_link=user.email)
    claims = SocialClaims(provider="google", subject="9", email=user.email, email_verified=True, hd=None)
    with pytest.raises(mfa.MfaRequired) as raised:
        services.social_login("google", claims, device_label="pytest")
    pending = mfa.open_challenge(raised.value.challenge.raw)
    assert pending is not None and pending.method == "google" and pending.identity_id is not None


# --- disable and new recovery codes ------------------------------------------------------------------------


def test_disable_needs_the_password_and_a_code(user: StaffUser) -> None:
    device, _ = enable(user)
    ops_staff(user.email, key=CONSOLE_KEY)
    client, _ = bearer(user)
    wrong_password = client.post(
        MFA + "disable/", {"current_password": "wrong", "code_or_recovery": totp_code(device)}, format="json"
    )
    assert (
        wrong_password.status_code == 400 and wrong_password.data["errors"][0]["code"] == "password_incorrect"
    )
    wrong_code = client.post(
        MFA + "disable/", {"current_password": TEST_PASSWORD, "code_or_recovery": "000001"}, format="json"
    )
    assert code_of(wrong_code) == "mfa_code_invalid"
    assert mfa.enabled(user)
    done = client.post(
        MFA + "disable/",
        {"current_password": TEST_PASSWORD, "code_or_recovery": totp_code(device)},
        format="json",
    )
    assert done.status_code == 200 and done.data == {"enabled": False, "recovery_codes_left": 0}
    assert AuditEntry.objects.get(action="auth.mfa_disabled").after == {
        "factor": "totp",
        "totp_devices": 1,
        "recovery_codes": 10,
    }
    assert twofactor.has_second_factor(user, scope=twofactor.CONSOLE)  # the console's device stays
    assert set(login(user.email).data) == LOGIN_KEYS  # no challenge any more
    off = client.post(
        MFA + "disable/", {"current_password": TEST_PASSWORD, "code_or_recovery": "1"}, format="json"
    )
    assert off.status_code == 409


def test_disable_with_a_recovery_code(user: StaffUser) -> None:
    _, codes = enable(user)
    client, _ = bearer(user)
    done = client.post(
        MFA + "disable/", {"current_password": TEST_PASSWORD, "code_or_recovery": codes[3]}, format="json"
    )
    assert done.status_code == 200 and not mfa.enabled(user)
    assert AuditEntry.objects.get(action="auth.mfa_disabled").after["factor"] == "recovery"  # type: ignore[index]


def test_new_recovery_codes_replace_the_old_ones(user: StaffUser) -> None:
    device, old = enable(user)
    client, _ = bearer(user)
    wrong = client.post(
        MFA + "recovery-codes/", {"current_password": TEST_PASSWORD, "code": "000001"}, format="json"
    )
    assert code_of(wrong) == "mfa_code_invalid"
    answer = client.post(
        MFA + "recovery-codes/", {"current_password": TEST_PASSWORD, "code": totp_code(device)}, format="json"
    )
    assert answer.status_code == 200 and len(answer.data["recovery_codes"]) == 10
    assert not set(answer.data["recovery_codes"]) & set(old)
    assert twofactor.use_recovery_code(user, old[0], scope=twofactor.PANEL) is None
    assert twofactor.use_recovery_code(user, answer.data["recovery_codes"][0], scope=twofactor.PANEL)
    assert AuditEntry.objects.get(action="auth.mfa_recovery_codes_regenerated").after == {
        "recovery_codes": 10
    }
    twofactor.reset_devices(user, scope=twofactor.PANEL)
    off = client.post(
        MFA + "recovery-codes/", {"current_password": TEST_PASSWORD, "code": "1"}, format="json"
    )
    assert off.status_code == 409


# --- the manager's reset and the CLI -----------------------------------------------------------------------


@ALERTS
def test_a_manager_resets_mfa(user: StaffUser, django_capture_on_commit_callbacks: Any) -> None:
    device, _ = enable(user)
    console = ops_staff(user.email, key=CONSOLE_KEY)
    open_session = AuthSessionFactory.create(staff=user)
    for _ in range(5):
        verify(challenge_for(user), code="000001")
    manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    client, _ = bearer(manager)
    lock = f"auth:lock:{mfa.GUARD_SCOPE}:{email_hash(user.email)}"
    assert cache.get(lock) is not None
    with django_capture_on_commit_callbacks(execute=True):
        answer = client.post(f"/api/v1/admin/staff/{user.pk}/reset-mfa/", format="json")
    assert answer.status_code == 200, answer.data
    assert cache.get(lock) is None  # the code lockout ended with the reset
    assert answer.data["mfa_enabled"] is False
    assert not mfa.enabled(user) and twofactor.has_second_factor(console, scope=twofactor.CONSOLE)
    open_session.refresh_from_db()
    assert open_session.revoke_reason == "admin_revoked"
    entry = AuditEntry.objects.get(action="auth.mfa_reset")
    assert entry.actor_id == manager.pk and entry.entity_id == str(user.pk)
    assert entry.after == {"totp_devices": 1, "recovery_codes": 10, "revoked_count": 1, "via": "api"}
    assert AuditEntry.objects.filter(action="auth.session_revoked", entity_id=str(open_session.pk)).exists()
    assert OutboxMessage.objects.filter(topic="alert.send", payload__event="auth.mfa_reset").exists()
    # the password alone signs in again
    assert set(login(user.email).data) == LOGIN_KEYS
    assert not TOTPDevice.objects.filter(pk=device.pk).exists()


def test_reset_mfa_refusals(user: StaffUser) -> None:
    manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    enable(manager)
    client, _ = bearer(manager)
    own = client.post(f"/api/v1/admin/staff/{manager.pk}/reset-mfa/", format="json")
    assert own.status_code == 409
    without = client.post(f"/api/v1/admin/staff/{user.pk}/reset-mfa/", format="json")
    assert without.status_code == 409
    assert client.post("/api/v1/admin/staff/999999/reset-mfa/", format="json").status_code == 404
    plain, _ = bearer(StaffUserFactory.create(email="plain@x.uz"))
    enable(user)
    assert plain.post(f"/api/v1/admin/staff/{user.pk}/reset-mfa/", format="json").status_code == 403
    assert mfa.enabled(user) and mfa.enabled(manager)


def test_only_a_superuser_resets_a_superusers_mfa(user: StaffUser) -> None:
    """Review SEC-2: a manager who could also e-mail or reset the password must not strip a superuser's second
    factor; another superuser (or the operator's ``resetstaffmfa``) can."""
    root = StaffUser.objects.create_superuser("root@x.uz", TEST_PASSWORD, full_name="Root")
    enable(root)
    path = f"/api/v1/admin/staff/{root.pk}/reset-mfa/"
    manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    refused = bearer(manager)[0].post(path, format="json")
    assert refused.status_code == 409
    assert (refused.data["code"], refused.data["message"]) == ("conflict", services.MFA_SUPERUSER_RESET)
    assert mfa.enabled(root) and not AuditEntry.objects.filter(action="auth.mfa_reset").exists()
    other = StaffUser.objects.create_superuser("root2@x.uz", TEST_PASSWORD, full_name="Root 2")
    assert bearer(other)[0].post(path, format="json").status_code == 200
    assert not mfa.enabled(root)
    enable(root)
    call_command("resetstaffmfa", root.email, "--actor", manager.email, stdout=StringIO())
    assert not mfa.enabled(root)


@ALERTS
def test_reset_mfa_keeps_the_optional_reason(user: StaffUser) -> None:
    manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    client, _ = bearer(manager)
    enable(user)
    path = f"/api/v1/admin/staff/{user.pk}/reset-mfa/"
    too_long = client.post(path, {"reason": "x" * 201}, format="json")
    assert too_long.status_code == 400
    assert [(error["field"], error["code"]) for error in too_long.json()["errors"]] == [
        ("reason", "max_length")
    ]
    assert mfa.enabled(user) and not AuditEntry.objects.filter(action="auth.mfa_reset").exists()
    session = AuthSessionFactory.create(staff=user)
    answer = client.post(path, {"reason": "  Telefon yo‘qolgan  "}, format="json")
    assert answer.status_code == 200, answer.data
    assert AuditEntry.objects.get(action="auth.mfa_reset").reason == "Telefon yo‘qolgan"
    revoked = AuditEntry.objects.get(action="auth.session_revoked", entity_id=str(session.pk))
    assert revoked.reason == "Telefon yo‘qolgan"
    # blank, or no body at all (the earlier clients), is an empty reason
    enable(user)
    assert client.post(path, {"reason": "   "}, format="json").status_code == 200
    enable(user)
    assert client.post(path, format="json").status_code == 200
    assert sorted(AuditEntry.objects.filter(action="auth.mfa_reset").values_list("reason", flat=True)) == [
        "",
        "",
        "Telefon yo‘qolgan",
    ]


def test_the_member_and_list_show_mfa(user: StaffUser) -> None:
    enable(user)
    ops_staff("ops@x.uz", key=CONSOLE_KEY)  # console only
    manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    client, _ = bearer(manager)
    assert client.get(f"/api/v1/admin/staff/{user.pk}/").data["mfa_enabled"] is True
    console_only = StaffUser.objects.get(email="ops@x.uz")
    assert client.get(f"/api/v1/admin/staff/{console_only.pk}/").data["mfa_enabled"] is False
    # The staff screen's rows carry it too (manager only; anyone else gets `null`).
    rows = {row["id"]: row for row in client.get("/api/v1/admin/staff/").data["results"]}
    assert (rows[user.pk]["mfa_enabled"], rows[console_only.pk]["mfa_enabled"]) == (True, False)
    plain_client, _ = bearer(StaffUserFactory.create(email="plain@x.uz"))
    plain = {row["id"]: row for row in plain_client.get("/api/v1/admin/staff/").data["results"]}
    assert plain[user.pk]["mfa_enabled"] is None


def test_the_cli_resets_mfa(user: StaffUser) -> None:
    enable(user)
    AuthSessionFactory.create(staff=user)
    out = StringIO()
    call_command("resetstaffmfa", user.email, stdout=out)
    assert "1 device(s), 10 recovery code(s) removed; 1 session(s) revoked" in out.getvalue()
    entry = AuditEntry.objects.get(action="auth.mfa_reset")
    assert entry.actor_type == "system" and entry.reason == "manage.py resetstaffmfa"
    assert entry.after["via"] == "cli"  # type: ignore[index]
    with pytest.raises(CommandError):
        call_command("resetstaffmfa", user.email, stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("resetstaffmfa", "nobody@x.uz", stdout=StringIO())


NEW_ACTIONS = (
    "auth.mfa_challenged",
    "auth.mfa_succeeded",
    "auth.mfa_failed",
    "auth.mfa_locked",
    "auth.mfa_enabled",
    "auth.mfa_disabled",
    "auth.mfa_recovery_used",
    "auth.mfa_recovery_codes_regenerated",
    "auth.mfa_reset",
)


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_the_new_actions_are_staff_sign_in_security(action: str) -> None:
    from glowderma.core import audit_scopes

    assert audit_scopes.scope_of(action, "staff", "staff") == audit_scopes.STAFF_SECURITY
    assert audit_scopes.scope_of(action, "system", "staff") == audit_scopes.STAFF_SECURITY
