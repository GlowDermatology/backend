# ruff: noqa: S105, S106 - test credentials and token-type literals
"""The dormant-account rule (customer contract §2.2.3, M2-A16) on reset and on an SMS-only social link."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.accounts.tests.factories import (
    AddressFactory,
    CustomerSocialIdentityFactory,
    StaffUserFactory,
    start_test_session,
)
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.commerce.models import CartItem
from glowderma.commerce.tests.factories import CartFactory, CartItemFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.customers import selectors, services
from glowderma.customers.tests.p1_support import (
    FID,
    NEW_PASSWORD,
    OTHER_FID,
    app_client,
    password_customer,
    reset_fakes,
    verification,
)
from glowderma.loyalty.tests.factories import LoyaltyAccountFactory, PurchaseFactory

pytestmark = pytest.mark.django_db
RESET = "/api/v1/auth/reset-password/"
SOCIAL = "/api/v1/auth/social/"
COMPLETE = "/api/v1/auth/social/complete/"
PHONE = "+998906667788"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def age(customer: Customer, days: int) -> None:
    """Make every sign-in of ``customer`` ``days`` old."""
    instant = timeutils.now() - timedelta(days=days)
    Customer.objects.filter(pk=customer.pk).update(last_login_at=instant)
    AuthSession.objects.filter(customer=customer).update(last_used_at=instant, created_at=instant)


def holder(days: int, *, password: bool = True) -> Customer:
    customer = password_customer(PHONE) if password else Customer.objects.create(phone=PHONE, name="Old")
    Customer.objects.filter(pk=customer.pk).update(terms_version="2025-01-01", consent_at=timeutils.now())
    start_test_session(customer, fid=OTHER_FID)
    AddressFactory.create(customer=customer)
    CartItemFactory.create(cart=CartFactory.create(customer=customer))
    age(customer, days)
    return customer


def reset() -> Any:
    token = verification(PHONE, "reset_password")
    return app_client(fid=FID, HTTP_ACCEPT_LANGUAGE="ru").post(
        RESET, {"verification_token": token, "password": NEW_PASSWORD}
    )


def test_reset_of_a_dormant_account_resets_it_as_at_deletion() -> None:
    customer = holder(181)
    LoyaltyAccountFactory.create(customer=customer, balance=5_000)
    purchase = PurchaseFactory.create(customer=customer, store=True, at=timeutils.now() - timedelta(days=400))
    before = timeutils.now()
    response = reset()
    assert response.status_code == 200, response.data
    customer.refresh_from_db()
    assert customer.history_from is not None and customer.history_from >= before
    assert (customer.name, customer.address_text, customer.language, customer.terms_version) == (
        "",
        "",
        "ru",
        "",
    )
    assert customer.consent_at is None
    assert not Address.objects.filter(customer=customer).exists()
    assert not CartItem.objects.filter(cart__customer=customer).exists()
    assert customer.loyalty_account.balance == 5_000
    assert customer.purchases.filter(pk=purchase.pk).exists()
    entry = AuditEntry.objects.get(action="customer.history_reset")
    assert dict(entry.after or {})["reason"] == "dormant"
    assert response.data["customer"]["registered_at"] == timeutils.isoformat(customer.history_from)


@pytest.mark.parametrize(
    "setup", ["recent_login", "recent_purchase", "staff_booking", "attended", "future_open"]
)
def test_activity_keeps_the_account(setup: str) -> None:
    customer = holder(200)
    now = timeutils.now()
    if setup == "recent_login":
        age(customer, 179)
    elif setup == "recent_purchase":
        PurchaseFactory.create(customer=customer, store=True, at=now - timedelta(days=7))
    elif setup == "staff_booking":
        appointment = AppointmentFactory.create(
            customer=customer,
            requested_via=enums.RequestedVia.ADMIN,
            created_by=StaffUserFactory.create(),
            start=now - timedelta(days=300),
        )
        type(appointment).objects.filter(pk=appointment.pk).update(created_at=now - timedelta(days=10))
    elif setup == "attended":
        appointment = AppointmentFactory.create(
            customer=customer,
            status=enums.AppointmentStatus.ATTENDED,
            start=now - timedelta(days=30),
            confirmed_at=now - timedelta(days=31),
            attended_at=now - timedelta(days=30),
        )
        type(appointment).objects.filter(pk=appointment.pk).update(created_at=now - timedelta(days=300))
    else:
        appointment = AppointmentFactory.create(
            customer=customer,
            status=enums.AppointmentStatus.CONFIRMED,
            start=now + timedelta(days=7),
            confirmed_at=now - timedelta(days=199),
        )
        type(appointment).objects.filter(pk=appointment.pk).update(created_at=now - timedelta(days=200))
    assert reset().status_code == 200
    customer.refresh_from_db()
    assert customer.history_from is None and customer.name == "Malika"
    assert not AuditEntry.objects.filter(action="customer.history_reset").exists()


def test_old_appointments_do_not_count_and_hide_behind_the_horizon() -> None:
    customer = holder(200)
    now = timeutils.now()
    appointment = AppointmentFactory.create(
        customer=customer,
        status=enums.AppointmentStatus.ATTENDED,
        start=now - timedelta(days=250),
        confirmed_at=now - timedelta(days=251),
        attended_at=now - timedelta(days=250),
    )
    type(appointment).objects.filter(pk=appointment.pk).update(created_at=now - timedelta(days=260))
    dormant, last = selectors.is_dormant(customer.pk, now=now)
    assert dormant is True and last is not None
    assert reset().status_code == 200
    customer.refresh_from_db()
    appointment.refresh_from_db()
    assert customer.history_from is not None and appointment.created_at < customer.history_from


def link_by_sms(token: str, provider: str = "google", **body: Any) -> Any:
    client = app_client(fid=FID)
    payload: dict[str, Any] = {"provider": provider, "token": token, "nonce": "*", **body}
    step = client.post(SOCIAL, payload)
    assert step.status_code == 200 and step.data["next_step"] == "verify_phone", step.data
    ticket = step.data["social_ticket"]
    jti = services.read_social_ticket(ticket, fid=FID).jti
    return client.post(
        COMPLETE, {"social_ticket": ticket, "verification_token": verification(PHONE, "link_social", jti=jti)}
    )


def test_dormant_social_only_account_locks_the_previous_holder_out() -> None:
    customer = holder(181, password=False)
    CustomerSocialIdentityFactory.create(customer=customer, provider="google", subject="g-old")
    old_device = Device.objects.get(customer=customer, fid=OTHER_FID)
    assert old_device.key_hash
    done = link_by_sms(
        "fake-apple:a-new:n@privaterelay.appleid.com", "apple", authorization_code="fake-code:1"
    )
    assert done.status_code == 200, done.data
    assert done.data["customer"]["linked_providers"] == ["apple"]
    assert done.data["customer"]["consent"]["terms_version"] is None
    unlinked = AuditEntry.objects.get(action="customer.social_unlinked")
    assert unlinked.after == {"provider": "google", "reason": "dormant"}
    old_device.refresh_from_db()
    assert old_device.key_hash == ""
    customer.refresh_from_db()
    assert customer.terms_version == "" and customer.consent_at is None
    # the old Google subject now needs the phone again
    later = app_client(fid="fid-later-0001").post(
        SOCIAL, {"provider": "google", "token": "fake:g-old:x@g.c", "nonce": "*"}
    )
    assert later.data["next_step"] == "verify_phone"


def test_dormant_previous_apple_holder_does_not_block_a_new_apple_identity() -> None:
    customer = holder(181, password=False)
    old = CustomerSocialIdentityFactory.create(
        customer=customer,
        provider="apple",
        subject="a-old",
        provider_refresh_token=account_services.seal_apple_refresh("r-old", client_id="uz.glowderma.app"),
        provider_client_id="uz.glowderma.app",
    )
    done = link_by_sms("fake-apple:a-new:n@x.y", "apple", authorization_code="fake-code:2")
    assert done.status_code == 200, done.data
    assert OutboxMessage.objects.filter(topic="apple.revoke", dedupe_key=f"apple.revoke:{old.pk}").exists()
    assert CustomerSocialIdentity.objects.get(customer=customer).subject == "a-new"


def test_active_social_only_account_keeps_its_identities() -> None:
    customer = holder(10, password=False)
    CustomerSocialIdentityFactory.create(customer=customer, provider="google", subject="g-keep")
    done = link_by_sms("fake:fb-1:a@x.uz", "facebook")
    assert done.status_code == 200, done.data
    assert sorted(done.data["customer"]["linked_providers"]) == ["facebook", "google"]
    assert not AuditEntry.objects.filter(action="customer.history_reset").exists()
    farewell = OutboxMessage.objects.get(topic="push.account_security").payload["farewell_tokens"]
    assert farewell == []  # the replaced device held no token
