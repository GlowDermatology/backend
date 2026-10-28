# ruff: noqa: S105, S106 - test credentials and token-type literals
"""``auth/send-code/`` and ``auth/verify-code/`` (customer contract §2.1.1, §2.1.2; plan §2 P1 "OTP")."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, SmsCode
from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage
from glowderma.customers.tests.p1_support import (
    OTHER_FID,
    app_client,
    password_customer,
    reset_fakes,
    sent_codes,
)
from glowderma.integrations.eskiz.registry import fake_eskiz

pytestmark = pytest.mark.django_db
SEND = "/api/v1/auth/send-code/"
VERIFY = "/api/v1/auth/verify-code/"
PHONE = "+998907770011"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A movable clock: ``clock[0]`` is now."""
    state = [timeutils.now()]
    monkeypatch.setattr(timeutils, "now", lambda: state[0])
    return state


def send(client: APIClient, phone: str = PHONE, purpose: str = "register", **extra: object) -> Any:
    return client.post(SEND, {"phone": phone, "purpose": purpose, **extra})


def test_send_code_issues_a_sealed_code_and_the_sms(django_capture_on_commit_callbacks: object) -> None:
    client = app_client()
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        response = send(client)
    assert response.status_code == 200, response.data
    assert response.data == {
        "phone_mask": "+998 90 *** ** 11",
        "email_mask": None,
        "purpose": "register",
        "expires_in": 120,
        "resend_in": 60,
        "attempts": 5,
        "length": 6,
    }
    row = SmsCode.objects.get()
    assert row.purpose == "signup" and row.sent_at is not None and row.provider_message_id == f"fake-{row.pk}"
    assert sent_codes() == ["123456"]
    assert fake_eskiz().sent[0].text == "Glow Derma: tasdiqlash kodingiz 123456. Kodni hech kimga bermang."
    # the handler deleted the sealed payload after the definitive outcome
    assert cache.get(account_services.otp_payload_key(row.pk)) is None


def test_no_plain_code_at_rest(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = app_client()
    assert send(client).status_code == 200
    row = SmsCode.objects.get()
    message = OutboxMessage.objects.get(topic="sms.send")
    assert message.payload == {"sms_code_id": row.pk, "lang": "uz", "platform": "ios"}
    assert "123456" not in str(message.payload)
    for field in SmsCode._meta.concrete_fields:
        assert "123456" not in str(getattr(row, field.attname))
    assert all("123456" not in record.getMessage() for record in caplog.records)
    sealed = cache.get(account_services.otp_payload_key(row.pk))
    assert isinstance(sealed, str) and "123456" not in sealed


def test_language_and_android_hash_line(django_capture_on_commit_callbacks: object) -> None:
    client = app_client(platform="android", HTTP_ACCEPT_LANGUAGE="ru")
    with override_settings(ANDROID_SMS_APP_HASH="AbCdEfGhIjK"):
        with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
            assert send(client).status_code == 200
    assert fake_eskiz().sent[0].text == (
        "Glow Derma: ваш код подтверждения 123456. Никому его не сообщайте.\nAbCdEfGhIjK"
    )


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"purpose": "register"}, "phone", "required"),
        ({"phone": "998901234567", "purpose": "register"}, "phone", "invalid"),
        ({"phone": PHONE}, "purpose", "required"),
        ({"phone": PHONE, "purpose": "login_new_device"}, "purpose", "invalid_choice"),
        ({"phone": PHONE, "purpose": "nope"}, "purpose", "invalid_choice"),
        ({"phone": PHONE, "purpose": "link_social"}, "social_ticket", "required"),
    ],
)
def test_send_code_validation(body: dict[str, Any], field: str, code: str) -> None:
    response = app_client().post(SEND, body)
    assert response.status_code == 400
    assert (response.data["errors"][0]["field"], response.data["errors"][0]["code"]) == (field, code)
    assert response.data["details"] is None


def test_phone_message_in_three_languages() -> None:
    body = {"phone": "12", "purpose": "register"}
    messages = {
        lang: app_client(HTTP_ACCEPT_LANGUAGE=lang).post(SEND, body).data["errors"][0]["message"]
        for lang in ("uz", "ru", "en")
    }
    assert messages == {
        "uz": "Telefon raqamini to‘g‘ri kiriting.",
        "ru": "Введите корректный номер телефона.",
        "en": "Please enter a valid phone number.",
    }


def test_unicode_digits_are_folded() -> None:
    response = app_client().post(SEND, {"phone": "+998９０７７７００１１", "purpose": "register"})
    assert response.status_code == 200
    assert SmsCode.objects.get().phone == PHONE


def test_phone_state_rules() -> None:
    password_customer(PHONE)
    response = send(app_client())
    assert (response.status_code, response.data["code"]) == (409, "phone_registered")
    assert response.data["message"] == "Bu raqam ro‘yxatdan o‘tgan. Hisobingizga kiring."
    response = send(app_client(), phone="+998907770012", purpose="reset_password")
    assert (response.status_code, response.data["code"]) == (409, "phone_not_registered")
    deleted = password_customer("+998907770013")
    account_services.delete_customer(deleted.pk)
    response = send(app_client(), phone="+998907770013", purpose="reset_password")
    assert response.data["code"] == "phone_not_registered"
    assert send(app_client(), phone="+998907770013", purpose="register").status_code == 200


def test_resend_window(clock: list[Any]) -> None:
    client = app_client()
    assert send(client).status_code == 200
    clock[0] += timedelta(seconds=59)
    response = send(client)
    assert (response.status_code, response.data["code"]) == (429, "otp_resend_too_soon")
    assert response["Retry-After"] == "1"
    assert response.data["details"] == {"retry_after": 1}
    assert response.data["message"] == "Yangi kodni 1 soniyadan so‘ng so‘rashingiz mumkin."
    clock[0] += timedelta(seconds=2)
    assert send(client).status_code == 200
    # the superseded code no longer verifies
    first, second = SmsCode.objects.order_by("id")
    assert first.expires_at <= second.created_at


@override_settings(THROTTLE_RATES={"sms_phone": "100/min", "sms_device": "100/min"})
def test_daily_cap_per_phone_and_tashkent_day(clock: list[Any]) -> None:
    clock[0] = clock[0].astimezone(timeutils.TASHKENT).replace(hour=23, minute=0, second=0, microsecond=0)
    client = app_client()
    for index in range(10):
        response = send(client, purpose="register") if index % 2 == 0 else send(client)
        assert response.status_code == 200, (index, response.data)
        clock[0] += timedelta(seconds=61)
        cache.clear()  # the fid counter is per device; this test is about the phone rows
    response = send(client)
    assert (response.status_code, response.data["code"]) == (429, "otp_daily_limit")
    assert int(response["Retry-After"]) == response.data["details"]["retry_after"] > 0
    clock[0] = clock[0].replace(hour=0, minute=1) + timedelta(days=1)
    assert send(client).status_code == 200


@override_settings(CUSTOMER_OTP_DEVICE_DAILY_LIMIT=2, THROTTLE_RATES={"sms_device": "100/min"})
def test_fid_cap(clock: list[Any]) -> None:
    client = app_client()
    for phone in ("+998907770021", "+998907770022"):
        assert send(client, phone=phone).status_code == 200
    response = send(client, phone="+998907770023")
    assert (response.status_code, response.data["code"]) == (429, "otp_daily_limit")
    assert send(app_client(fid=OTHER_FID), phone="+998907770023").status_code == 200


@override_settings(CUSTOMER_OTP_IP_DAILY_LIMIT=2)
def test_ip_cap_has_its_own_code_and_message() -> None:
    for index, phone in enumerate(("+998907770031", "+998907770032")):
        assert send(app_client(fid=f"fid-ip-cap-{index:04d}"), phone=phone).status_code == 200
    response = send(app_client(fid="fid-ip-cap-9999"), phone="+998907770033")
    assert (response.status_code, response.data["code"]) == (429, "otp_network_limit")
    assert response.data["message"].startswith("Bu tarmoqdan bugun juda ko‘p kod so‘raldi.")


@override_settings(SMS_GLOBAL_HOURLY_LIMIT=1)
def test_global_budget_trips_with_an_error_log(caplog: pytest.LogCaptureFixture) -> None:
    assert send(app_client()).status_code == 200
    with caplog.at_level(logging.ERROR):
        response = send(app_client(fid=OTHER_FID), phone="+998907770041")
    assert (response.status_code, response.data["code"]) == (503, "sms_unavailable")
    assert int(response["Retry-After"]) == response.data["details"]["retry_after"]
    assert any("otp.budget_tripped" in record.getMessage() for record in caplog.records)


@override_settings(SMS_GLOBAL_HOURLY_LIMIT=1, ALERT_WEBHOOK_URL="https://alerts.example.uz/hook")
def test_global_budget_trip_raises_one_alert_per_window() -> None:
    assert send(app_client()).status_code == 200
    for phone in ("+998907770042", "+998907770043"):
        assert send(app_client(fid=OTHER_FID), phone=phone).status_code == 503
    (alert,) = OutboxMessage.objects.filter(topic="alert.send")
    assert (alert.payload["event"], alert.payload["fields"]) == ("otp.budget_tripped", {"limit": 1})


def code_of(phone: str = PHONE) -> str:
    return "123456"


def verify(client: APIClient, code: str, phone: str = PHONE, purpose: str = "register") -> Any:
    return client.post(VERIFY, {"phone": phone, "purpose": purpose, "code": code})


def test_verify_issues_a_single_use_token() -> None:
    client = app_client()
    send(client)
    response = verify(client, "123456")
    assert response.status_code == 200, response.data
    assert set(response.data) == {"verification_token", "expires_in", "purpose", "customer_exists"}
    assert response.data["expires_in"] == 600 and response.data["customer_exists"] is False
    assert len(response.data["verification_token"]) == 43
    row = SmsCode.objects.get()
    assert row.consumed_at is not None
    assert row.verification_token_hash == account_services.hash_secret(response.data["verification_token"])
    again = verify(client, "123456")
    assert (again.status_code, again.data["code"]) == (400, "otp_expired")


def test_wrong_code_counts_and_locks(clock: list[Any]) -> None:
    client = app_client()
    send(client)
    for left in (4, 3, 2, 1):
        response = verify(client, "000000")
        assert (response.status_code, response.data["code"]) == (400, "otp_invalid")
        assert response.data["details"] == {"attempts_left": left}
        assert response.data["errors"] == [
            {
                "field": "code",
                "code": "otp_invalid",
                "message": f"Kod noto‘g‘ri. Yana {left} marta urinishingiz mumkin.",
            }
        ]
    locked = verify(client, "000000")
    assert (locked.status_code, locked.data["code"]) == (429, "otp_locked")
    assert locked.data["message"] == "Urinishlar soni tugadi. 15 daqiqadan so‘ng qayta urinib ko‘ring."
    assert SmsCode.objects.get().exhausted_at is not None
    # the right code no longer helps, and send-code is locked for 15 minutes
    assert verify(client, "123456").data["code"] == "otp_locked"
    assert send(client).data["code"] == "otp_locked"
    clock[0] += timedelta(minutes=16)
    assert send(client).status_code == 200


def test_verify_validation_and_expiry(clock: list[Any]) -> None:
    client = app_client()
    response = verify(client, "12ab56")
    assert response.data["errors"][0] == {
        "field": "code",
        "code": "invalid",
        "message": "6 xonali kodni kiriting.",
    }
    assert verify(client, "123456").data["code"] == "otp_expired"  # no code at all
    send(client)
    clock[0] += timedelta(seconds=121)
    assert verify(client, "123456").data["code"] == "otp_expired"


@override_settings(THROTTLE_RATES={"sms_phone": "100/min", "sms_device": "100/min"})
def test_daily_wrong_code_cap_across_codes(clock: list[Any]) -> None:
    client = app_client()
    for _round in range(2):
        send(client)
        for _ in range(4):
            assert verify(client, "000000").data["code"] == "otp_invalid"
        clock[0] += timedelta(seconds=61)
    send(client)
    assert verify(client, "000000").data["details"] == {"attempts_left": 1}
    final = verify(client, "000000")
    assert final.data["code"] == "otp_locked"
    assert send(client).data["code"] == "otp_locked"


def test_customer_exists_flag() -> None:
    password_customer(PHONE)
    client = app_client()
    assert send(client, purpose="reset_password").status_code == 200
    response = verify(client, "123456", purpose="reset_password")
    assert response.data["customer_exists"] is True


def test_consume_verification_rules(clock: list[Any]) -> None:
    client = app_client()
    send(client)
    token = verify(client, "123456").data["verification_token"]
    from django.db import transaction

    from glowderma.core.api.exceptions import VerificationInvalid

    with pytest.raises(VerificationInvalid), transaction.atomic():
        account_services.consume_verification(token, purpose="reset_password")
    with pytest.raises(VerificationInvalid), transaction.atomic():
        account_services.consume_verification(token, purpose="register", phone="+998900000000")
    with transaction.atomic():
        assert account_services.consume_verification(token, purpose="register", phone=PHONE)[0] == PHONE
    with pytest.raises(VerificationInvalid), transaction.atomic():
        account_services.consume_verification(token, purpose="register")
    send_later = app_client(fid=OTHER_FID)
    clock[0] += timedelta(seconds=61)
    send(send_later)
    token = verify(send_later, "123456").data["verification_token"]
    clock[0] += timedelta(minutes=11)
    with pytest.raises(VerificationInvalid), transaction.atomic():
        account_services.consume_verification(token, purpose="register")


def test_send_code_with_eskiz_rejected_and_down(django_capture_on_commit_callbacks: object) -> None:
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        assert send(app_client(), phone="+998907770000").status_code == 200
    rejected = SmsCode.objects.get(phone="+998907770000")
    assert rejected.sent_at is not None and rejected.provider_message_id == ""
    assert OutboxMessage.objects.get(dedupe_key=f"sms.send:{rejected.pk}").status == "done"


# --- races (transaction=True, threads)


def _run_threads(target: object, count: int = 2) -> list[Any]:
    results: list[Any] = []
    start = threading.Event()

    def worker() -> None:
        try:
            start.wait(10)
            results.append(target())  # type: ignore[operator]
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_send_code_one_sms() -> None:
    def call() -> int:
        return int(send(app_client()).status_code)

    statuses = sorted(_run_threads(call))
    assert statuses == [200, 429]
    assert SmsCode.objects.filter(phone=PHONE).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_verify_one_token() -> None:
    send(app_client())

    def call() -> Any:
        response = verify(app_client(), "123456")
        return response.status_code, response.data.get("code")

    results = sorted(_run_threads(call), key=str)
    assert [status for status, _ in results] == [200, 400]
    assert ("400", "otp_expired") in [(str(status), code) for status, code in results]
    assert Customer.objects.count() == 0
