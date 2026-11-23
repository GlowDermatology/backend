"""Customers dashboard reads of ``accounts``: sign-ups by method, phone coverage, sessions, one-time codes."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from glowderma.accounts import selectors
from glowderma.accounts.models import Device, SmsCode
from glowderma.accounts.tests.factories import (
    CustomerFactory,
    CustomerSessionFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
    SmsCodeFactory,
)
from glowderma.core import audit, enums
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db
DAY = datetime(2026, 10, 30, 0, 0, tzinfo=TASHKENT)
NOW = DAY + timedelta(days=1, hours=12)


def _email_customer(n: int, registered: datetime, **extra: Any) -> Any:
    return CustomerFactory.create(
        phone=None,
        email=f"user{n}@example.uz",
        email_verified_at=registered,
        registered_at=registered,
        **extra,
    )


def _attached(customer: Any) -> None:
    audit.record(
        action=selectors.PHONE_ATTACHED_ACTION,
        entity_type="customer",
        entity_id=str(customer.pk),
        actor=customer,
        actor_type="customer",
    )


def test_signups_by_day_and_method(django_assert_num_queries: Any) -> None:
    morning = DAY + timedelta(hours=9)
    CustomerFactory.create(registered_at=morning)  # phone
    CustomerFactory.create(registered_at=morning + timedelta(hours=1))  # phone
    _email_customer(1, morning)  # e-mail, still phoneless
    later_phone = CustomerFactory.create(
        email="later@example.uz", email_verified_at=morning, registered_at=morning
    )
    _attached(later_phone)  # signed up by e-mail, attached the phone afterwards
    google = _email_customer(2, DAY + timedelta(days=1, hours=8))
    CustomerSocialIdentityFactory.create(customer=google, provider="google", created_at=google.registered_at)
    linked_later = CustomerFactory.create(registered_at=morning)  # phone sign-up, Google linked a week later
    CustomerSocialIdentityFactory.create(customer=linked_later, created_at=morning + timedelta(days=7))
    CustomerFactory.create(registered_at=DAY - timedelta(minutes=1))  # outside
    with django_assert_num_queries(1):
        rows = selectors.signups_by_day_and_method(DAY, DAY + timedelta(days=2))
    assert rows == {
        (date(2026, 10, 30), "phone"): 3,
        (date(2026, 10, 30), "email"): 2,
        (date(2026, 10, 31), "google"): 1,
    }


def test_phone_coverage(django_assert_num_queries: Any) -> None:
    _email_customer(1, DAY)
    _email_customer(2, DAY)
    _email_customer(3, DAY, status=enums.CustomerStatus.DELETED, deleted_at=DAY)  # not active: not counted
    attached = CustomerFactory.create()
    _attached(attached)
    CustomerFactory.create()
    with django_assert_num_queries(1):
        coverage = selectors.phone_coverage()
    assert (coverage.phoneless, coverage.attached_later, coverage.active) == (2, 1, 4)


def test_open_customer_sessions_by_platform(django_assert_num_queries: Any) -> None:
    for platform in (Device.Platform.IOS, Device.Platform.IOS, Device.Platform.WEB):
        device = DeviceFactory.create(platform=platform)
        CustomerSessionFactory.create(customer=device.customer, device=device)
    CustomerSessionFactory.create()  # no device
    CustomerSessionFactory.create(revoked_at=NOW - timedelta(hours=1), revoke_reason="logout")
    CustomerSessionFactory.create(absolute_expires_at=NOW - timedelta(seconds=1))
    with django_assert_num_queries(1):
        sessions = selectors.open_customer_sessions_by_platform(NOW)
    assert sessions == {"ios": 2, "web": 1, "unknown": 1}


def test_otp_volume_by_channel_and_purpose(django_assert_num_queries: Any) -> None:
    hour_ago, two_days_ago = NOW - timedelta(hours=1), NOW - timedelta(days=2)
    SmsCodeFactory.create(created_at=hour_ago, consumed_at=hour_ago)
    SmsCodeFactory.create(created_at=two_days_ago, attempts=5, exhausted_at=two_days_ago)
    SmsCodeFactory.create(created_at=hour_ago, attempts=5, exhausted_at=hour_ago)
    SmsCodeFactory.create(
        phone="", email="a@example.uz", purpose=SmsCode.Purpose.VERIFY_EMAIL, created_at=hour_ago
    )
    SmsCodeFactory.create(created_at=NOW - timedelta(days=8))  # older than a week
    with django_assert_num_queries(1):
        rows = selectors.otp_volume(NOW)
    found = {(row.channel, row.purpose): row for row in rows}
    assert set(found) == {("sms", "signup"), ("email", "verify_email")}
    sms = found[("sms", "signup")]
    assert (sms.last_day, sms.last_week, sms.verified_week, sms.locked_day, sms.locked_week) == (
        2,
        3,
        1,
        1,
        2,
    )
    assert found[("email", "verify_email")].last_week == 1
