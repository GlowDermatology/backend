# ruff: noqa: S105, S106 - test credentials and token-type literals
"""Registration, login with the new-device code, reset, logout (customer contract §2.1.3–§2.1.5, §2.1.7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.hashers import MD5PasswordHasher
from django.core.cache import cache
from django.test import override_settings

from glowderma.accounts import services as account_services
from glowderma.accounts.models import AuthSession, Customer, CustomerSocialIdentity, Device, SmsCode
from glowderma.accounts.tests.factories import CustomerSocialIdentityFactory, start_test_session
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.customers.tests.p1_support import (
    FID,
    NEW_PASSWORD,
    OTHER_FID,
    PASSWORD,
    TERMS,
    app_client,
    bearer,
    password_customer,
    publish_terms,
    reset_fakes,
    verification,
)
from glowderma.loyalty.tests.factories import LoyaltyAccountFactory, PurchaseFactory


def device_id(started: Any) -> int:
    value = started.session.device_id
    assert value is not None
    return int(value)


def _strip(data: Any) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "request_id"}


pytestmark = pytest.mark.django_db
SET_PASSWORD = "/api/v1/auth/set-password/"
LOGIN = "/api/v1/auth/login/"
RESET = "/api/v1/auth/reset-password/"
LOGOUT = "/api/v1/auth/logout/"
ME = "/api/v1/me/"
PHONE = "+998901112233"
TOKEN_KEYS = {
    "access",
    "access_expires_at",
    "refresh",
    "refresh_expires_at",
    "device_key",
    "customer",
    "is_new_customer",
    "reactivated",
}


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def register_body(token: str, **overrides: Any) -> dict[str, Any]:
    return {
        "verification_token": token,
        "password": PASSWORD,
        "name": "Malika",
        "terms_version": TERMS,
    } | overrides


# --- registration


def test_register_new_customer() -> None:
    publish_terms()
    token = verification(PHONE, "register")
    response = app_client(REMOTE_ADDR="203.0.113.9").post(SET_PASSWORD, register_body(token, language="ru"))
    assert response.status_code == 201, response.data
    assert set(response.data) == TOKEN_KEYS
    assert response.data["is_new_customer"] is True and response.data["reactivated"] is False
    assert len(response.data["device_key"]) == 43
    customer = Customer.objects.get(phone=PHONE)
    assert (customer.name, customer.language, customer.terms_version) == ("Malika", "ru", TERMS)
    assert customer.consent_ip == "203.0.113.9" and customer.consent_at is not None
    assert response.data["customer"]["has_password"] is True
    assert response.data["customer"]["consent"] == {
        "terms_version": TERMS,
        "accepted_at": timeutils.isoformat(customer.consent_at),
        "current_version": TERMS,
        "update_required": False,
    }
    assert AuditEntry.objects.get(action="customer.registered").after == {
        "reactivated": False,
        "terms_version": TERMS,
    }
    session = AuthSession.objects.get(customer=customer)
    assert session.auth_method == "password"
    # the access token works and the token is single use
    me = bearer(app_client(), response.data["access"]).get(ME)
    assert me.status_code == 200 and me.data["phone"] == PHONE
    again = app_client().post(SET_PASSWORD, register_body(token))
    assert again.data["code"] == "verification_invalid"


def test_register_reactivates_a_deleted_customer_with_balance_and_purchases() -> None:
    publish_terms()
    customer = password_customer(PHONE)
    LoyaltyAccountFactory.create(customer=customer, balance=12_000)
    purchase = PurchaseFactory.create(customer=customer, store=True)
    account_services.delete_customer(customer.pk)
    token = verification(PHONE, "register")
    response = app_client().post(SET_PASSWORD, register_body(token, name="Dilnoza"))
    assert response.status_code == 201, response.data
    assert response.data["reactivated"] is True and response.data["is_new_customer"] is False
    customer.refresh_from_db()
    assert customer.status == "active" and customer.reactivated_at is not None
    assert customer.history_from == customer.reactivated_at
    assert response.data["customer"]["id"] == customer.pk
    assert response.data["customer"]["registered_at"] == timeutils.isoformat(customer.reactivated_at)
    assert customer.loyalty_account.balance == 12_000
    assert customer.purchases.filter(pk=purchase.pk).exists()


def test_register_refusals() -> None:
    token = verification(PHONE, "register")
    response = app_client().post(SET_PASSWORD, register_body(token))
    assert (response.status_code, response.data["code"]) == (503, "terms_not_published")
    publish_terms()
    assert app_client().post(SET_PASSWORD, register_body("nope")).data["code"] == "verification_invalid"
    outdated = app_client().post(SET_PASSWORD, register_body(token, terms_version="2025-01-01"))
    assert outdated.data["errors"][0]["code"] == "terms_outdated"
    assert outdated.data["errors"][0]["message"] == "Foydalanish shartlarining yangi tahririni qabul qiling."
    short_name = app_client().post(SET_PASSWORD, register_body(token, name=" A "))
    assert short_name.data["errors"][0] == {
        "field": "name",
        "code": "min_length",
        "message": "Ismingizni kiriting (kamida 2 belgi).",
    }
    password_customer(PHONE)
    taken = app_client().post(SET_PASSWORD, register_body(token))
    assert (taken.status_code, taken.data["code"]) == (409, "phone_registered")


@pytest.mark.parametrize(
    ("password", "code", "message"),
    [
        ("Ab1!", "password_too_short", "Parol kamida 8 ta belgidan iborat bo‘lsin."),
        ("password1", "password_too_common", "Bu parol juda oddiy."),
        ("81723649105", "password_entirely_numeric", "Parol faqat raqamlardan iborat bo‘lmasin."),
        ("998901112233a", "password_too_similar", "Parol telefon raqamingizga juda o‘xshash."),
    ],
)
def test_register_password_rules(password: str, code: str, message: str) -> None:
    publish_terms()
    token = verification(PHONE, "register")
    response = app_client().post(SET_PASSWORD, register_body(token, password=password))
    assert response.status_code == 400
    assert {"field": "password", "code": code, "message": message} in response.data["errors"]


def test_password_rule_in_russian_and_english() -> None:
    publish_terms()
    token = verification(PHONE, "register")
    ru = app_client(HTTP_ACCEPT_LANGUAGE="ru").post(SET_PASSWORD, register_body(token, password="Ab1!"))
    en = app_client(HTTP_ACCEPT_LANGUAGE="en").post(SET_PASSWORD, register_body(token, password="Ab1!"))
    assert ru.data["errors"][0]["message"] == "Пароль должен быть не короче 8 символов."
    assert en.data["errors"][0]["message"] == "The password must be at least 8 characters."
    assert ru["Content-Language"] == "ru"


# --- login


def known_device(customer: Customer) -> str:
    started = start_test_session(customer, fid=FID)
    assert started.raw_device_key is not None
    return started.raw_device_key


def login(
    phone: str = PHONE, password: str = PASSWORD, *, key: str | None = None, fid: str = FID, **body: Any
) -> Any:
    return app_client(fid=fid, key=key).post(LOGIN, {"phone": phone, "password": password, **body})


def test_login_known_device_gets_tokens_without_new_key() -> None:
    customer = password_customer(PHONE)
    key = known_device(customer)
    response = login(key=key)
    assert response.status_code == 200, response.data
    assert response.data["device_key"] is None
    assert AuditEntry.objects.filter(action="customer.login_succeeded").latest("id").after == {
        "method": "password",
        "new_device": False,
    }


def test_login_unknown_device_asks_for_the_code_then_signs_in(
    django_capture_on_commit_callbacks: Any,
) -> None:
    customer = password_customer(PHONE)
    old = start_test_session(customer, fid=OTHER_FID)
    known_device(customer)  # fid known, but this client sends no key
    step = login()
    assert step.status_code == 200, step.data
    assert step.data == {
        "next_step": "verify_device",
        "phone_mask": "+998 90 *** ** 33",
        "otp": {"expires_in": 120, "resend_in": 60, "length": 6},
        "social_ticket": None,
        "provider": None,
        "email_mask": None,
        "expires_in": 120,
    }
    row = SmsCode.objects.get(purpose="new_device")
    # a second call within 60 s reuses the live code
    assert login().data["otp"]["resend_in"] <= 60
    assert SmsCode.objects.filter(purpose="new_device").count() == 1
    token = verification(PHONE, "login_new_device")
    response = login(verification_token=token)
    assert response.status_code == 200, response.data
    assert len(response.data["device_key"]) == 43
    assert (
        dict(AuditEntry.objects.filter(action="customer.login_succeeded").latest("id").after or {})[
            "new_device"
        ]
        is True
    )
    old.session.refresh_from_db()
    assert old.session.revoke_reason == "replaced"
    assert row.purpose == "new_device"
    bad = login(verification_token="nope")
    assert bad.data["code"] == "verification_invalid"


def test_login_wrong_password_and_unknown_phone_are_identical() -> None:
    password_customer(PHONE)
    wrong = login(password="Wrong-password-1")
    unknown = login(phone="+998909999998")
    assert wrong.status_code == unknown.status_code == 401
    assert (
        _strip(wrong.data)
        == _strip(unknown.data)
        == {
            "code": "invalid_credentials",
            "message": "Telefon raqami yoki parol noto‘g‘ri.",
            "errors": [],
            "details": None,
        }
    )
    assert AuditEntry.objects.filter(action="customer.login_failed").count() == 2


def count_hashes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = MD5PasswordHasher.verify

    def spy(self: MD5PasswordHasher, password: str, encoded: str) -> bool:
        calls[0] += 1
        return original(self, password, encoded)

    monkeypatch.setattr(MD5PasswordHasher, "verify", spy)
    return calls


def test_login_timing_parity_one_hash_each(monkeypatch: pytest.MonkeyPatch) -> None:
    password_customer(PHONE)
    deleted = password_customer("+998901112234")
    account_services.delete_customer(deleted.pk)
    Customer.objects.create(phone="+998901112235", name="Social")  # no password
    from glowderma.customers import services

    services._dummy_hash()  # computed once, outside the measurement
    for phone, password in (
        ("+998909999997", PASSWORD),
        ("+998901112234", PASSWORD),
        ("+998901112235", PASSWORD),
        (PHONE, "Wrong-password-1"),
    ):
        calls = count_hashes(monkeypatch)
        assert login(phone=phone, password=password).data["code"] == "invalid_credentials"
        assert calls[0] == 1, phone
        monkeypatch.undo()


def test_login_guard_locks_and_device_guard_is_separate() -> None:
    customer = password_customer(PHONE)
    key = known_device(customer)
    for _ in range(5):
        assert login(password="Wrong-password-1").status_code == 401
    assert login(password="Wrong-password-1").status_code == 429
    assert login().status_code == 429  # the unknown-device path is locked for this phone
    assert login(key=key).status_code == 200  # the owner's known device is not
    for _ in range(5):
        assert login(key=key, password="Wrong-password-1").status_code == 401
    assert login(key=key).status_code == 429
    cache.delete_many([k for k in ("x",)])


@pytest.mark.parametrize(
    ("setting", "code", "status"),
    [
        ({"CUSTOMER_OTP_DAILY_LIMIT": 1}, "otp_daily_limit", 429),
        ({"CUSTOMER_OTP_DEVICE_DAILY_LIMIT": 1}, "otp_daily_limit", 429),
        ({"CUSTOMER_OTP_IP_DAILY_LIMIT": 1}, "otp_network_limit", 429),
        ({"SMS_GLOBAL_HOURLY_LIMIT": 1}, "sms_unavailable", 503),
    ],
)
def test_capped_login_is_no_password_oracle(
    setting: dict[str, int], code: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    customer = password_customer(PHONE)
    with override_settings(**setting):
        # exhaust the cap with one code for this phone/device/IP
        assert login().status_code == 200
        SmsCode.objects.update(created_at=timeutils.now() - timedelta(minutes=5))
        calls = count_hashes(monkeypatch)
        right = login()
        wrong = login(password="Wrong-password-1")
        checked = calls[0]
        unknown = login(phone="+998909999996")
    assert checked == 0
    for response in (right, wrong):
        assert (response.status_code, response.data["code"]) == (status, code)
        assert response.data["details"] == right.data["details"]
    if code != "otp_daily_limit" or "CUSTOMER_OTP_DEVICE_DAILY_LIMIT" in setting:
        assert unknown.data["code"] == code
    assert customer.pk


def test_lockout_via_wrong_codes_blocks_login_before_password() -> None:
    password_customer(PHONE)
    now = timeutils.now()
    SmsCode.objects.create(
        phone=PHONE, purpose="new_device", code_hmac="0" * 64, expires_at=now, exhausted_at=now, attempts=5
    )
    assert login().data["code"] == "otp_locked"
    assert login(password="nope-nope-1").data["code"] == "otp_locked"


# --- reset


def test_reset_password_revokes_unlinks_and_notifies(django_capture_on_commit_callbacks: Any) -> None:
    customer = password_customer(PHONE)
    old = start_test_session(customer, fid=OTHER_FID)
    Device.objects.filter(pk=device_id(old)).update(fcm_token="fcm-old-device", push_enabled=True)
    CustomerSocialIdentityFactory.create(customer=customer, provider="google")
    apple = CustomerSocialIdentityFactory.create(
        customer=customer,
        provider="apple",
        provider_refresh_token=account_services.seal_apple_refresh("r1", client_id="uz.glowderma.app"),
        provider_client_id="uz.glowderma.app",
    )
    token = verification(PHONE, "reset_password")
    response = app_client().post(RESET, {"verification_token": token, "password": NEW_PASSWORD})
    assert response.status_code == 200, response.data
    assert len(response.data["device_key"]) == 43
    customer.refresh_from_db()
    assert account_services.check_customer_password(customer, NEW_PASSWORD)
    old.session.refresh_from_db()
    assert old.session.revoke_reason == "password_changed"
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()
    assert sorted(
        str(dict(e.after or {}).get("provider"))
        for e in AuditEntry.objects.filter(action="customer.social_unlinked")
    ) == ["apple", "google"]
    unlinked = AuditEntry.objects.get(action="customer.social_unlinked", after__provider="apple")
    assert dict(unlinked.after or {})["reason"] == "password_reset"
    assert OutboxMessage.objects.filter(topic="apple.revoke", dedupe_key=f"apple.revoke:{apple.pk}").exists()
    assert Device.objects.get(pk=device_id(old)).key_hash == ""
    security = OutboxMessage.objects.get(topic="push.account_security")
    assert set(security.payload) == {"customer_id", "event", "event_id", "provider", "farewell_tokens"}
    assert security.payload["event"] == "password_reset" and security.payload["provider"] == ""
    assert security.payload["farewell_tokens"] == ["fcm-old-device"]
    assert len(security.payload["event_id"]) == 32
    assert security.dedupe_key.endswith(security.payload["event_id"])
    assert AuditEntry.objects.filter(action="customer.password_reset").exists()
    # the old device now gets 401 password_changed with its message
    from glowderma.accounts import tokens

    access, _ = tokens.issue_access(old.session)
    stale = bearer(app_client(fid=OTHER_FID), access, fid=OTHER_FID).get(ME)
    assert (stale.status_code, stale.data["code"]) == (401, "password_changed")


def test_reset_refusals() -> None:
    customer = password_customer(PHONE)
    token = verification(PHONE, "reset_password")
    same = app_client().post(RESET, {"verification_token": token, "password": PASSWORD})
    assert same.data["errors"][0]["code"] == "password_unchanged"
    assert app_client().post(RESET, {"verification_token": "x", "password": NEW_PASSWORD}).data["code"] == (
        "verification_invalid"
    )
    account_services.delete_customer(customer.pk)
    gone = app_client().post(RESET, {"verification_token": token, "password": NEW_PASSWORD})
    assert (gone.status_code, gone.data["code"]) == (409, "phone_not_registered")


# --- logout


def test_logout_revokes_and_clears_the_device_token() -> None:
    customer = password_customer(PHONE)
    started = start_test_session(customer, fid=FID)
    Device.objects.filter(pk=device_id(started)).update(fcm_token="fcm-1", push_enabled=True)
    response = app_client().post(LOGOUT, {"refresh": started.raw_refresh})
    assert response.status_code == 204
    started.session.refresh_from_db()
    assert started.session.revoke_reason == "logout"
    device = Device.objects.get(pk=device_id(started))
    assert (device.is_active, device.push_enabled, device.fcm_token) == (False, False, "")
    assert device.key_hash  # kept: signing in again on this install skips the SMS
    assert AuditEntry.objects.filter(action="customer.logout").exists()
    again = app_client().post(LOGOUT, {"refresh": started.raw_refresh})
    assert (again.status_code, again.data["code"]) == (401, "SESSION_EXPIRED")
    assert app_client().post(LOGOUT, {"refresh": "junk"}).data["code"] == "SESSION_EXPIRED"


def test_auth_routes_require_device_headers() -> None:
    from rest_framework.test import APIClient

    response = APIClient().post(LOGIN, {"phone": PHONE, "password": PASSWORD}, format="json")
    assert (response.status_code, response.data["code"]) == (400, "device_required")
