"""Security hardening of the ops console (glowderma.accounts): secrets in outbox payloads, the 2FA reset and
enrolment window, the per-IP budgets of the sign-in steps."""

from __future__ import annotations

import re
from datetime import timedelta
from io import StringIO
from typing import Any
from urllib.parse import quote

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import Client, RequestFactory, override_settings
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts import services as account_services
from glowderma.accounts import topics
from glowderma.accounts.api.guard import client_bucket
from glowderma.accounts.console import access, state, twofactor
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import (
    TEST_PASSWORD,
    CustomerFactory,
    CustomerSocialIdentityFactory,
    StaffUserFactory,
)
from glowderma.accounts.tests.ops_helpers import ops_login, ops_staff, totp_code
from glowderma.core import outbox, timeutils
from glowderma.core.jsonmask import MASK
from glowderma.core.models import AuditEntry, OutboxMessage

pytestmark = pytest.mark.django_db


# --- finding 2: outbox payloads with credentials -----------------------------------------------------------

# One payload shape per registered outbox topic, as its producer writes it. A value ending in ``-SECRET`` is a
# credential and must never reach the change page; ``order_id``-like ids must stay readable.
TOPIC_PAYLOADS: dict[str, dict[str, Any]] = {
    "alert.send": {
        "event": "outbox.dead",
        "at": "2026-10-31T10:00:00+05:00",
        "fields": {"n": 1},
        "host": "w1",
    },
    "apple.revoke": {
        "identity_id": 11,
        "customer_id": 12,
        "subject": "apple-sub",
        "client_id": "uz.glowderma.app",
        "sealed_token": "APPLE-SEALED-SECRET",
    },
    "billz.push_sale": {"purchase_id": 3, "order_id": 21, "billz_order_uuid": "8f0e…"},
    "billz.stock_reread": {"order_id": 22, "product_ids": [1, 2]},
    "bts.create_shipment": {"order_id": 23, "manual": False, "request_no": 1, "actor_id": None},
    "campaign.send_batch": {"campaign_id": 4, "batch_no": 0},
    "catalog.stock_synced": {"job_id": 5},
    "email.notice": {"to": "sealed-address", "lang": "uz", "kind": "password_changed"},
    "email.send": {"sms_code_id": 6, "lang": "uz", "purpose": "signup"},
    "job.queued": {"job_id": 7, "kind": "billz_import"},
    "push.account_security": {
        "customer_id": 13,
        "event": "password_reset",
        "event_id": "e" * 32,
        "provider": "",
        "farewell_tokens": ["FCM-ONE-SECRET", "FCM-TWO-SECRET"],
    },
    "push.appointment": {
        "appointment_id": 8,
        "action": "confirmed",
        "event_id": 9,
        "customer_id": 14,
        "reschedule_no": None,
    },
    "push.order_status": {"order_id": 24, "status": "processing", "event_id": 10, "customer_id": 15},
    "push.session_replaced": {"session_id": "s-1", "sid": "s-1", "fcm_token": "FCM-REPLACED-SECRET"},
    "refund.submit": {"refund_id": 1, "payment_id": 2, "order_id": 25, "provider_txn_id": "t-1", "amount": 9},
    "sms.send": {"sms_code_id": 16, "lang": "uz", "platform": "ios"},
}


def change_page(client: Client, message: OutboxMessage) -> str:
    answer = client.get(f"/django-admin/core/outboxmessage/{message.pk}/change/")
    assert answer.status_code == 200
    return answer.content.decode()


def test_the_sample_table_covers_every_registered_topic() -> None:
    assert set(TOPIC_PAYLOADS) == set(outbox._handlers), "add the new topic's payload shape above"


@pytest.mark.parametrize("topic", sorted(TOPIC_PAYLOADS))
def test_no_outbox_payload_shows_a_credential(ops_client: Client, topic: str) -> None:
    payload = TOPIC_PAYLOADS[topic]
    message = OutboxMessage.objects.create(topic=topic, payload=payload, dedupe_key=f"{topic}:probe")
    content = change_page(ops_client, message)
    assert "-SECRET" not in content
    secrets = [key for key, value in payload.items() if "SECRET" in str(value)]
    if secrets:
        assert MASK in content
    for key, value in payload.items():
        if key.endswith("_id") and isinstance(value, int):
            assert f"&quot;{key}&quot;: {value}" in content or f'"{key}": {value}' in content, key


def test_the_real_payload_builders_are_masked_on_the_change_page(ops_client: Client) -> None:
    customer = CustomerFactory.create()
    identity = CustomerSocialIdentityFactory.create(
        customer=customer,
        provider="apple",
        provider_refresh_token="SEALED-REFRESH-MARKER",  # noqa: S106
    )
    assert account_services.enqueue_apple_revoke(identity)
    assert account_services.enqueue_apple_token_revoke(
        "SEALED-ORPHAN-MARKER", client_id="uz.glowderma.app", customer_id=customer.pk, subject="other-sub"
    )
    payload = topics.account_security_payload(customer.pk, "password_reset", farewell_tokens=["FCM-MARKER"])
    outbox.enqueue(
        topics.PUSH_ACCOUNT_SECURITY,
        payload,
        dedupe_key=topics.account_security_key(customer.pk, "password_reset", payload["event_id"]),
    )
    messages = list(OutboxMessage.objects.order_by("pk"))
    assert [m.topic for m in messages] == ["apple.revoke", "apple.revoke", "push.account_security"]
    for message in messages:
        content = change_page(ops_client, message)
        assert "MARKER" not in content and MASK in content
        assert f"{customer.pk}" in content  # the customer id stays readable


# --- finding 3: the 2FA reset, the enrolment window, four eyes ---------------------------------------------

LOGIN = "/django-admin/login/"
VERIFY = "/django-admin/2fa/"
ENROL = "/django-admin/2fa/enrol/"
CODES = "/django-admin/2fa/recovery-codes/"
STAFF_CHANGELIST = "/django-admin/accounts/staffuser/"


def password_step(client: Client, email: str, password: str = TEST_PASSWORD) -> Any:
    return client.post(LOGIN, {"username": email, "password": password})


def is_verified_console(client: Client) -> bool:
    return client.get("/django-admin/").status_code == 200


def shift_clock(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    moved = timeutils.now() + timedelta(seconds=seconds)
    monkeypatch.setattr(timeutils, "now", lambda: moved)


def enrol_through_forms(client: Client, user: StaffUser, password: str = TEST_PASSWORD) -> Any:
    """Password step, enrolment page, confirmation; returns the confirmation's answer."""
    assert password_step(client, user.email, password)["Location"] == ENROL
    page = client.get(ENROL)
    if page.status_code != 200:
        return page
    device = TOTPDevice.objects.get(user=user, confirmed=False)
    return client.post(ENROL, {"code": totp_code(device)})


def console_reset(manager_client: Client, target: StaffUser) -> Any:
    return manager_client.post(
        STAFF_CHANGELIST,
        {"action": "reset_two_factor", "_selected_action": [target.pk], "post": "yes"},
        follow=True,
    )


def refused_reason(user: StaffUser) -> str:
    entry = AuditEntry.objects.filter(action="console.enrol_refused", entity_id=str(user.pk)).last()
    return entry.reason if entry is not None else ""


def test_a_2fa_reset_ends_pending_and_active_console_sessions() -> None:
    target = ops_staff("target@x.uz")
    pending, active = Client(), Client()
    assert password_step(pending, target.email)["Location"] == VERIFY
    ops_login(active, target)
    result = access.reset_two_factor(target, actor=None, reason="test", via=access.VIA_CLI)
    assert result["sessions"] == 2
    assert not Session.objects.exists()
    # the pending sign-in is gone: it cannot continue into the enrolment the reset opened
    assert pending.get(ENROL)["Location"] == LOGIN
    assert not is_verified_console(active)
    entry = AuditEntry.objects.get(action="ops_access.2fa_reset")
    assert entry.after["sessions"] == 2 and entry.after["via"] == "cli"  # type: ignore[index]


def test_without_a_window_the_password_alone_cannot_enrol() -> None:
    user = StaffUserFactory.create(email="new@x.uz", is_staff=True)  # access without a grant entry
    client = Client()
    assert password_step(client, user.email)["Location"] == ENROL
    answer = client.get(ENROL, follow=True)
    assert answer.redirect_chain == [(LOGIN, 302)]
    assert "Menejerdan 2FA ni qayta o‘rnatishni so‘rang" in answer.content.decode()
    assert not TOTPDevice.objects.filter(user=user).exists()
    assert client.post(ENROL, {"code": "123456"})["Location"] == LOGIN  # the pending sign-in ended
    assert not is_verified_console(client)
    assert refused_reason(user) == access.NO_WINDOW


def test_grant_opens_a_window_that_expires_and_closes_on_enrolment(monkeypatch: pytest.MonkeyPatch) -> None:
    user = StaffUserFactory.create(email="new@x.uz")
    assert access.grant(user, actor=None, reason="test")
    window = access.enrolment_window(user)
    assert window is not None and window.action == "ops_access.granted"
    assert (window.until - window.opened_at).total_seconds() == settings.OPS_ENROL_WINDOW_SECONDS
    # expired: refused
    shift_clock(monkeypatch, settings.OPS_ENROL_WINDOW_SECONDS + 1)
    assert access.enrolment_window(user) is None
    assert enrol_through_forms(Client(), user)["Location"] == LOGIN
    monkeypatch.undo()
    # in time: enrols, and the enrolment closes the window
    assert enrol_through_forms(Client(), user)["Location"] == CODES
    assert access.enrolment_window(user) is None
    entry = AuditEntry.objects.get(action="console.2fa_enrolled")
    assert entry.after["window_action"] == "ops_access.granted"  # type: ignore[index]
    assert entry.after["window_opened_by"] == "system"  # type: ignore[index]
    # a device lost without a reset (deleted by hand): no new enrolment until a manager resets 2FA
    twofactor.reset_devices(user)
    assert enrol_through_forms(Client(), user)["Location"] == LOGIN


def test_grant_on_an_account_with_access_but_no_device_opens_a_new_window() -> None:
    user = StaffUserFactory.create(email="old@x.uz", is_staff=True)
    assert access.enrolment_window(user) is None
    out = StringIO()
    call_command("ops_access", "grant", "--email", user.email, stdout=out)
    assert "enrolment is open until" in out.getvalue()
    assert AuditEntry.objects.filter(action="ops_access.enrolment_opened").count() == 1
    assert access.enrolment_window(user) is not None
    assert enrol_through_forms(Client(), user)["Location"] == CODES


def test_revoke_closes_the_window() -> None:
    user = StaffUserFactory.create(email="new@x.uz")
    access.grant(user, actor=None, reason="test")
    access.revoke(user, actor=None, reason="test")
    access.grant(user, actor=None, reason="again")
    assert access.enrolment_window(user) is not None  # a new grant opens a new one
    access.revoke(user, actor=None, reason="test")
    assert access.enrolment_window(user) is None


@override_settings(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook")
def test_the_console_reset_opens_a_window_and_the_enrolment_is_audited_and_alerted() -> None:
    manager = ops_staff("boss@x.uz", is_manager=True)
    target = ops_staff("target@x.uz")
    manager_client = Client()
    ops_login(manager_client, manager)
    done = console_reset(manager_client, target)
    assert "konsol seanslari tugatildi" in done.content.decode()
    window = access.enrolment_window(target)
    assert window is not None and window.via == "console" and window.opened_by_id == manager.pk
    assert enrol_through_forms(Client(), target)["Location"] == CODES
    entry = AuditEntry.objects.get(action="console.2fa_enrolled")
    assert entry.after["window_audit_id"] == window.audit_id  # type: ignore[index]
    assert entry.after["window_opened_by"] == manager.full_name  # type: ignore[index]
    alert = OutboxMessage.objects.get(topic="alert.send", payload__event="console.2fa_enrolled")
    assert alert.payload["fields"]["staff_id"] == target.pk


@pytest.mark.parametrize("password_first", [True, False])
def test_one_manager_cannot_reset_both_factors_and_enrol(password_first: bool) -> None:
    manager = ops_staff("boss@x.uz", is_manager=True)
    target = ops_staff("target@x.uz")
    manager_client = Client()
    ops_login(manager_client, manager)
    temporary = ""
    if password_first:
        temporary, _ = account_services.reset_staff_password(target, actor=manager)
        ops_login(manager_client, manager)
    console_reset(manager_client, target)
    if not password_first:
        temporary, _ = account_services.reset_staff_password(target, actor=manager)
    answer = enrol_through_forms(Client(), target, temporary)
    assert answer["Location"] == LOGIN
    assert refused_reason(target) == access.SAME_MANAGER
    assert not TOTPDevice.objects.filter(user=target).exists()
    # another manager (or the operator's CLI) opening the window lets the person enrol
    other = ops_staff("boss2@x.uz", is_manager=True)
    other_client = Client()
    ops_login(other_client, other)
    console_reset(other_client, target)
    assert enrol_through_forms(Client(), target, temporary)["Location"] == CODES


def test_the_cli_reset_is_not_subject_to_four_eyes() -> None:
    manager = ops_staff("boss@x.uz", is_manager=True)
    target = ops_staff("target@x.uz")
    temporary, _ = account_services.reset_staff_password(target, actor=manager)
    out = StringIO()
    call_command("ops_access", "reset-2fa", "--email", target.email, "--actor", manager.email, stdout=out)
    assert "console session(s) ended" in out.getvalue()
    assert enrol_through_forms(Client(), target, temporary)["Location"] == CODES


def test_a_password_reset_by_a_manager_ends_the_persons_console_sessions() -> None:
    manager = ops_staff("boss@x.uz", is_manager=True)
    target = ops_staff("target@x.uz")
    pending, active = Client(), Client()
    password_step(pending, target.email)
    ops_login(active, target)
    account_services.reset_staff_password(target, actor=manager)
    assert not Session.objects.exists()
    assert not is_verified_console(active)
    entry = AuditEntry.objects.get(action="staff.password_reset")
    assert entry.after["console_sessions"] == 2  # type: ignore[index]


# --- recovery codes page, CSRF page, small sign-in fixes ---------------------------------------------------


def test_recovery_codes_are_shown_once_on_a_get_page_with_copy_and_download() -> None:
    user = StaffUserFactory.create(email="new@x.uz")
    access.grant(user, actor=None, reason="test")
    client = Client()
    assert enrol_through_forms(client, user)["Location"] == CODES
    page = client.get(CODES)
    content = page.content.decode()
    codes = re.findall(r"<code>([a-z2-7]{5}-[a-z2-7]{5})</code>", content)
    assert page.status_code == 200 and len(codes) == 10
    assert "no-store" in page["Cache-Control"]
    assert 'src="/static/ops/recovery-codes.js"' in content and "data-copy-target" in content
    assert 'download="glowderma-ops-zaxira-kodlar.txt"' in content
    assert f"data:text/plain;charset=utf-8,{quote('Glow Derma Ops')}" in content
    assert all(quote(code) in content for code in codes)  # the download carries the same codes
    assert state.RECOVERY_CODES not in client.session
    again = client.get(CODES)  # a reload: never the codes again, a clear message and a way on
    assert again.status_code == 200
    assert codes[0] not in again.content.decode()
    assert "faqat bir marta ko‘rsatiladi" in again.content.decode()
    assert 'href="/django-admin/"' in again.content.decode()


def test_the_recovery_codes_page_needs_a_verified_session() -> None:
    assert Client().get(CODES)["Location"] == LOGIN


def test_a_refused_csrf_check_answers_with_the_uzbek_console_page() -> None:
    ops_staff("ops@x.uz")
    client = Client(enforce_csrf_checks=True)
    client.get(LOGIN)
    answer = client.post(LOGIN, {"username": "ops@x.uz", "password": TEST_PASSWORD})
    assert answer.status_code == 403
    content = answer.content.decode()
    assert "Sahifa eskirgan" in content and "Glow Derma Ops" in content
    assert "CSRF" not in content and "Forbidden" not in content
    assert f'href="{LOGIN}"' in content


def test_a_wrong_code_is_not_kept_in_the_field() -> None:
    user = ops_staff("ops@x.uz")
    good = totp_code(TOTPDevice.objects.get(user=user))
    wrong = "000000" if good != "000000" else "111111"
    client = Client()
    password_step(client, user.email)
    answer = client.post(VERIFY, {"code": wrong})
    content = answer.content.decode()
    assert "Kod noto‘g‘ri" in content and f'value="{wrong}"' not in content


def test_the_login_heading_has_no_stray_colon() -> None:
    content = Client().get(LOGIN).content.decode()
    assert "Xush kelibsiz" in content and "Xush kelibsiz:" not in content


# --- finding 6: per-address budgets of the sign-in steps ---------------------------------------------------


def wrong_code_for(user: StaffUser) -> str:
    good = totp_code(TOTPDevice.objects.get(user=user))
    return "000000" if good != "000000" else "111111"


@override_settings(OPS_IP_FAILURE_LIMIT=5, ALERT_WEBHOOK_URL="https://alerts.example.uz/hook")
def test_the_password_step_has_a_per_address_budget_across_accounts() -> None:
    user = ops_staff("ops@x.uz")
    client = Client()
    for index in range(5):  # five different accounts: no single account reaches its own ladder
        assert password_step(client, f"guess{index}@x.uz", "wrong-password").status_code == 200
    locked = password_step(client, user.email)  # even the right password, from this address
    assert locked.status_code == 429 and int(locked["Retry-After"]) == settings.OPS_IP_FAILURE_WINDOW_SECONDS
    assert "Juda ko‘p urinish" in locked.content.decode()
    entry = AuditEntry.objects.get(action="console.ip_locked")
    assert entry.after["step"] == "password" and entry.ip == "127.0.0.1"  # type: ignore[index]
    alert = OutboxMessage.objects.get(topic="alert.send", payload__event="console.ip_locked")
    assert alert.payload["fields"] == {"step": "password", "wait": settings.OPS_IP_FAILURE_WINDOW_SECONDS}
    # another address is not affected
    assert password_step(Client(REMOTE_ADDR="10.1.1.1"), user.email)["Location"] == VERIFY


@override_settings(OPS_IP_FAILURE_LIMIT=5)
def test_successful_sign_ins_give_their_attempt_back() -> None:
    user = ops_staff("ops@x.uz")
    for _ in range(8):
        assert password_step(Client(), user.email)["Location"] == VERIFY
    assert password_step(Client(), "nobody@x.uz", "wrong").status_code == 200


@override_settings(OPS_IP_FAILURE_LIMIT=5)
def test_the_code_steps_have_a_per_address_budget_across_accounts() -> None:
    users = [ops_staff(f"ops{index}@x.uz") for index in range(3)]
    clients = [Client() for _ in users]
    for client, user in zip(clients, users, strict=True):
        assert password_step(client, user.email)["Location"] == VERIFY
    statuses = [
        clients[index % 3].post(VERIFY, {"code": wrong_code_for(users[index % 3])}).status_code
        for index in range(5)
    ]
    assert statuses == [200] * 5  # at most two failures per account: no account ladder
    good = totp_code(TOTPDevice.objects.get(user=users[2]))
    locked = clients[2].post(VERIFY, {"code": good})
    assert locked.status_code == 429 and not is_verified_console(clients[2])
    assert f'value="{good}"' not in locked.content.decode()
    entry = AuditEntry.objects.get(action="console.ip_locked")
    assert entry.after["step"] == "code"  # type: ignore[index]
    assert not AuditEntry.objects.filter(action="console.otp_locked").exists()
    # the password step has its own budget, and another address its own
    other = Client(REMOTE_ADDR="10.1.1.1")
    password_step(other, users[2].email)
    assert other.post(VERIFY, {"code": totp_code(TOTPDevice.objects.get(user=users[2]))}).status_code == 302


@override_settings(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook")
def test_an_account_code_lockout_is_alerted() -> None:
    user = ops_staff("ops@x.uz")
    client = Client()
    password_step(client, user.email)
    wrong = wrong_code_for(user)
    for _ in range(5):
        client.post(VERIFY, {"code": wrong})
    assert client.post(VERIFY, {"code": wrong}).status_code == 429
    alert = OutboxMessage.objects.get(topic="alert.send", payload__event="console.otp_locked")
    assert alert.payload["fields"]["staff_id"] == user.pk


@pytest.mark.parametrize(
    ("remote", "bucket"),
    [
        ("10.1.2.3", "10.1.2.3"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::9", "2001:db8:1:2::/64"),
        ("not-an-ip", "unknown"),
    ],
)
def test_the_budget_key_groups_an_ipv6_client_by_its_64(remote: str, bucket: str) -> None:
    assert client_bucket(RequestFactory().get("/", REMOTE_ADDR=remote)) == bucket
