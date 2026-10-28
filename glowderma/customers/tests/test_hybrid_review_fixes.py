# ruff: noqa: S105, S106 - test credentials
"""Regression tests of the B-W2 review: account-wide login and OTP budgets, reactivation by phone only, the
last-sign-in-method rule of a password without a phone or e-mail, the per-IP e-mail cap, mailbox
normalisation and the permanent 0011 migration checks."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.test import override_settings
from rest_framework.exceptions import ValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts.api.throttles import canonical_email
from glowderma.accounts.models import Customer, CustomerSocialIdentity, SmsCode
from glowderma.accounts.tests.factories import CustomerFactory, start_test_session
from glowderma.core import timeutils
from glowderma.core.api.exceptions import (
    AuthFailed,
    EmailRegistered,
    LastSignInMethod,
    OtpLocked,
    OtpNetworkLimit,
)
from glowderma.core.pii import normalize_email
from glowderma.customers import services
from glowderma.integrations.email import registry as email_registry
from glowderma.site_config.models import SiteSettings

pytestmark = pytest.mark.django_db
PASSWORD = "Saffron!Harbor-Long72"
PHONE = "+998901250001"
EMAIL = "both@example.uz"


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    cache.clear()
    email_registry.fake_email().sent.clear()
    yield
    email_registry.fake_email().sent.clear()


def ctx(fid: str = "fid-review-0001", ip: str = "203.0.113.9") -> services.DeviceContext:
    return services.DeviceContext(fid=fid, platform="ios", app_version="1.0.0", ip=ip, lang="uz")


def both() -> Customer:
    customer = account_services.create_customer(PHONE, name="Ikki Yo‘l", language="uz", email=EMAIL)
    account_services.set_customer_password(customer, PASSWORD)
    return customer


def test_the_login_guard_is_per_account_once_the_identifier_resolves() -> None:
    both()
    for _ in range(5):
        with pytest.raises(AuthFailed):
            services.login(PHONE, "Wrong-pass-123", None, device=ctx(), device_key=None)
    from rest_framework.exceptions import Throttled

    with pytest.raises(Throttled):  # the e-mail is the same account: no second budget
        services.login(EMAIL, "Wrong-pass-123", None, device=ctx(), device_key=None)
    with pytest.raises(AuthFailed):  # an unknown identifier keeps its own key: nothing revealed
        services.login("nobody@example.uz", "Wrong-pass-123", None, device=ctx(), device_key=None)


def test_otp_lockout_and_wrong_codes_are_per_account() -> None:
    both()
    now = timeutils.now()
    SmsCode.objects.create(
        phone=PHONE, purpose="reset", code_hmac="x", created_at=now, expires_at=now, exhausted_at=now
    )
    with pytest.raises(OtpLocked):  # the phone's lockout locks the account's e-mail too
        account_services.send_code(
            EMAIL, "reset_password", ip="", fid="fid-review-0002", lang="uz", platform="ios"
        )
    SmsCode.objects.all().delete()
    SmsCode.objects.create(
        email=EMAIL,
        purpose="reset",
        code_hmac="x",
        created_at=now,
        expires_at=now,
        attempts=5,
        max_attempts=10,
    )
    assert account_services.wrong_codes_today(PHONE, now) == 5
    assert account_services.wrong_codes_today("+998901259999", now) == 0


def test_an_email_code_never_reactivates_a_deleted_account(monkeypatch: pytest.MonkeyPatch) -> None:
    SiteSettings.objects.filter(pk=1).update(
        terms_version="v1", terms_uz="Shartlar", privacy_policy_uz="Maxfiy"
    )
    from glowderma.site_config.selectors import invalidate_public_settings

    invalidate_public_settings()
    held = CustomerFactory.create(email="old@example.uz", email_verified_at=timeutils.now())
    Customer.objects.filter(pk=held.pk).update(status="deleted", deleted_at=timeutils.now())  # a scrub race
    account_services.send_code(
        "old@example.uz", "register", ip="", fid="fid-review-0003", lang="uz", platform="ios"
    )
    token = account_services.verify_code("old@example.uz", "register", "123456").token
    with pytest.raises(EmailRegistered):
        services.register(
            token,
            password=PASSWORD,
            name="Yangi",
            terms_version="v1",
            language="uz",
            device=ctx("fid-review-0003"),
        )
    assert Customer.objects.get(pk=held.pk).status == "deleted"


def test_a_password_without_a_phone_or_email_is_no_sign_in_method() -> None:
    customer = CustomerFactory.create(phone=None)
    account_services.set_customer_password(customer, PASSWORD)
    CustomerSocialIdentity.objects.create(customer=customer, provider="google", subject="sub-review-1")
    started = start_test_session(customer)
    with pytest.raises(LastSignInMethod):
        services.unlink_social(
            customer.pk, "google", password=PASSWORD, verification_token=None, sid=str(started.session.pk)
        )


def test_one_ip_cannot_exhaust_the_email_budget() -> None:
    with override_settings(EMAIL_OTP_IP_HOURLY_LIMIT=2):
        for index in range(2):
            account_services.send_code(
                f"a{index}@example.uz",
                "register",
                ip="198.51.100.7",
                fid=f"fid-review-1{index}00",
                lang="uz",
                platform="ios",
            )
        with pytest.raises(OtpNetworkLimit):
            account_services.send_code(
                "a9@example.uz",
                "register",
                ip="198.51.100.7",
                fid="fid-review-1900",
                lang="uz",
                platform="ios",
            )
        account_services.send_code(  # another IP, and SMS codes, are not affected
            "a9@example.uz", "register", ip="198.51.100.8", fid="fid-review-1901", lang="uz", platform="ios"
        )
        account_services.send_code(
            PHONE, "register", ip="198.51.100.7", fid="fid-review-1902", lang="uz", platform="ios"
        )


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        (" Ali@Пример.РФ ", "ali@xn--e1afmkfd.xn--p1ai"),
        ("Ali@BÜCHER.de", "ali@xn--bcher-kva.de"),
        ("Café@example.uz", "café@example.uz"),  # NFD → NFC
        ("ali+shop@example.uz", "ali+shop@example.uz"),  # plus-addressing kept
    ],
)
def test_one_mailbox_has_one_stored_form(raw: str, stored: str) -> None:
    assert normalize_email(raw) == stored
    assert account_services.normalize_email(raw) == stored
    assert canonical_email(raw) in (stored, None)  # the validator may refuse an exotic local part
    assert canonical_email("ALI@xn--e1afmkfd.xn--p1ai") == canonical_email("ali@пример.рф")


def test_unicode_and_punycode_spellings_are_one_account() -> None:
    first = account_services.create_customer(None, name="A", language="uz", email="ali@пример.рф")
    with pytest.raises(Exception):  # noqa: B017, PT011 - the unique key (IntegrityError)
        account_services.create_customer(None, name="B", language="uz", email="ALI@XN--E1AFMKFD.XN--P1AI")
    assert account_services.active_customer_by_email("Ali@Пример.рф") == first


def test_password_rules_still_validate_after_the_reauth_change() -> None:
    with pytest.raises(ValidationError):
        services.validate_customer_password("both@example", EMAIL)


@pytest.mark.django_db(transaction=True)
def test_0011_forward_and_reverse_and_the_previous_release_writer() -> None:
    call_command("migrate", "accounts", "0010", verbosity=0)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'accounts_smscode'"
        )
        assert "email" not in {row[0] for row in cursor.fetchall()}
    call_command("migrate", "accounts", verbosity=0)
    # a writer of the previous release inserts an SMS code without the e-mail column (deploy window)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_smscode (phone, purpose, code_hmac, attempts, max_attempts, created_at,"
            " expires_at, verification_token_hash, provider_message_id)"
            " VALUES (%s, 'signup', 'x', 0, 5, now(), now(), '', '') RETURNING id",
            [PHONE],
        )
        row_id = cursor.fetchone()[0]
    assert SmsCode.objects.get(pk=row_id).email == ""
    SmsCode.objects.create(
        email="x@example.uz",
        purpose="signup",
        code_hmac="x",
        created_at=timeutils.now(),
        expires_at=timeutils.now(),
    )
    with pytest.raises(IrreversibleError, match="1 e-mail or attach code"):
        call_command("migrate", "accounts", "0010", verbosity=0)
    SmsCode.objects.filter(email="x@example.uz").delete()
    call_command("migrate", "accounts", "0010", verbosity=0)
    call_command("migrate", verbosity=0)
