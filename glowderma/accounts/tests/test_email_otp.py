"""E-mail OTP (hybrid identity): the ``email.send`` handler, the e-mail breaker, the shared engine rules, the
scrubs, the audit label of a phoneless customer and the 0011 reverse guard."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db.migrations.exceptions import IrreversibleError
from django.test import override_settings

from glowderma.accounts import services, tasks
from glowderma.accounts.models import Customer, SmsCode
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import (
    EmailServiceUnavailable,
    OtpDailyLimit,
    OtpResendTooSoon,
    PhoneRequired,
)
from glowderma.core.models import OutboxMessage
from glowderma.integrations.email import registry as email_registry
from glowderma.integrations.email.fake import FakeEmail
from glowderma.integrations.email.ports import EmailMessage, EmailUnavailable

pytestmark = pytest.mark.django_db
EMAIL = "malika@example.uz"


@pytest.fixture(autouse=True)
def clean_fake() -> Any:
    email_registry.fake_email().sent.clear()
    yield
    email_registry.fake_email().sent.clear()


def issue(email: str = EMAIL, purpose: str = "register", lang: str = "uz") -> OutboxMessage:
    services.send_code(email, purpose, ip="203.0.113.2", fid="fid-email-0001", lang=lang, platform="ios")
    row = SmsCode.objects.filter(email=email.strip().lower()).latest("id")
    return OutboxMessage.objects.get(dedupe_key=f"email.send:{row.pk}")


def test_an_email_code_is_an_hmac_row_with_a_sealed_payload_and_no_address_in_the_outbox() -> None:
    message = issue("  Malika@Example.UZ ")
    row = SmsCode.objects.get()
    assert (row.phone, row.email, row.purpose) == ("", EMAIL, "signup")
    assert (
        message.topic == "email.send"
        and EMAIL not in str(message.payload)
        and "123456" not in str(message.payload)
    )
    assert row.code_hmac == services.code_hmac("123456")
    tasks.send_email_code(message)
    [sent] = email_registry.fake_email().sent
    assert sent.to == EMAIL and "123456" in sent.text and sent.reference == f"sms_code_id={row.pk}"
    row.refresh_from_db()
    assert row.sent_at is not None and row.provider_message_id == f"fake-sms_code_id={row.pk}"
    assert cache.get(services.otp_payload_key(row.pk)) is None
    tasks.send_email_code(message)  # a redelivery never sends twice
    assert len(email_registry.fake_email().sent) == 1


@pytest.mark.parametrize(
    ("lang", "text"), [("ru", "Ваш код для регистрации"), ("en", "Your code to sign up")]
)
def test_email_texts_follow_the_request_language(lang: str, text: str) -> None:
    tasks.send_email_code(issue(lang=lang))
    [sent] = email_registry.fake_email().sent
    assert text in sent.text and text in sent.html
    subject, plain, html = tasks.email_parts("654321", "xx", "unknown")
    assert (
        subject == "Glow Derma: tasdiqlash kodi" and "Emailingizni tasdiqlash" in plain and "654321" in html
    )


def test_relay_outcomes_unclaim_or_keep_the_claim(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    down = issue("x@down.test")
    with pytest.raises(EmailUnavailable):
        tasks.send_email_code(down)
    assert SmsCode.objects.get(email="x@down.test").sent_at is None  # retried by the outbox
    rejected = issue("x@rejected.test")
    with caplog.at_level(logging.WARNING):
        tasks.send_email_code(rejected)
    assert SmsCode.objects.get(email="x@rejected.test").sent_at is not None
    assert "x@rejected.test" not in caplog.text

    def unknown(self: FakeEmail, message: EmailMessage) -> str:
        raise EmailUnavailable("timeout", sent=True)

    monkeypatch.setattr(FakeEmail, "send", unknown)
    lost = issue("lost@example.uz")
    tasks.send_email_code(lost)  # outcome unknown: kept claimed, never resent
    assert SmsCode.objects.get(email="lost@example.uz").sent_at is not None


def test_missing_or_broken_payloads_send_nothing() -> None:
    message = issue()
    row = SmsCode.objects.get()
    cache.set(services.otp_payload_key(row.pk), "not-sealed", 60)
    tasks.send_email_code(message)
    cache.delete(services.otp_payload_key(row.pk))
    tasks.send_email_code(message)
    SmsCode.objects.filter(pk=row.pk).delete()
    tasks.send_email_code(message)
    assert email_registry.fake_email().sent == []


def test_email_codes_share_the_engine_limits() -> None:
    issue()
    with pytest.raises(OtpResendTooSoon):
        issue()
    assert services.wrong_codes_today(EMAIL, timeutils.now()) == 0
    with (
        override_settings(CUSTOMER_OTP_DAILY_LIMIT=1, CUSTOMER_OTP_RESEND_SECONDS=1),
        pytest.raises(OtpDailyLimit) as capped,
    ):
        SmsCode.objects.update(created_at=timeutils.now() - timedelta(seconds=5))
        issue()
    assert capped.value.key == "error.otp_daily_limit_email"
    with pytest.raises(ValueError, match="by sms only"):
        services.send_code(EMAIL, "attach_phone", ip="", fid="fid-email-0002", lang="uz", platform="ios")


def test_the_email_breaker_is_separate_from_the_sms_one(caplog: pytest.LogCaptureFixture) -> None:
    with override_settings(EMAIL_GLOBAL_HOURLY_LIMIT=1), caplog.at_level(logging.ERROR):
        issue()
        with pytest.raises(EmailServiceUnavailable) as tripped:
            issue("second@example.uz")
    assert tripped.value.status_code == 503 and tripped.value.details["retry_after"] >= 1
    assert "otp.email_budget_tripped limit=1" in caplog.text
    services.send_code("+998901239876", "register", ip="", fid="fid-email-0003", lang="uz", platform="ios")


def test_phoneless_helpers_scrubs_and_audit_label() -> None:
    customer = services.create_customer(None, name="Dilnoza", language="uz", email="Dilnoza@Example.uz")
    assert (customer.phone, customer.email) == (None, "dilnoza@example.uz") and customer.email_verified_at
    assert str(customer) == "Dilnoza · d***@example.uz"
    assert audit.actor_identity(customer) == ("customer", "d***@example.uz")
    assert audit.actor_identity(Customer(name="x")) == ("customer", "***")
    with pytest.raises(PhoneRequired):
        services.require_phone(customer)
    assert services.active_customer_by_email(" DILNOZA@example.uz") == customer
    services.set_customer_phone(customer.pk, "+998901239870")
    services.require_phone(Customer.objects.get(pk=customer.pk))
    services.delete_customer(customer.pk)
    scrubbed = Customer.objects.get(pk=customer.pk)
    assert (scrubbed.phone, scrubbed.email, scrubbed.email_verified_at) == ("+998901239870", None, None)
    dormant = CustomerFactory.create(email="old@example.uz", email_verified_at=timeutils.now())
    services.scrub_profile_for_dormant(dormant.pk, language="ru")
    assert Customer.objects.get(pk=dormant.pk).email is None


@pytest.mark.django_db(transaction=True)
def test_0011_reverse_refuses_while_it_would_lose_phoneless_accounts_or_email_codes() -> None:
    customer = services.create_customer(None, name="Kamola", language="uz")
    with pytest.raises(IrreversibleError, match="1 customer"):
        call_command("migrate", "accounts", "0010", verbosity=0)
    customer.delete()
    call_command("migrate", "accounts", "0010", verbosity=0)
    call_command("migrate", verbosity=0)
