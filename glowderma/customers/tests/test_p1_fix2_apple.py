# ruff: noqa: S105, S106 - test credentials and token-type literals
"""Fix pass 2 regressions: Apple refresh tokens the code exchange produced for a refused request are revoked
(also when the request's Idempotency-Key transaction rolls back), never while their subject is linked
(Apple's revocation ends the whole authorization), and every ``apple.revoke`` message carries a non-secret
reference of its grant (customer contract §2.1.8, §2.1.9, §2.2.8)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts import tasks
from glowderma.accounts.models import Customer, CustomerSocialIdentity
from glowderma.accounts.tests.factories import CustomerSocialIdentityFactory, start_test_session
from glowderma.core import outbox
from glowderma.core.models import OutboxMessage
from glowderma.core.pii import pii_ref
from glowderma.customers import services
from glowderma.customers.tests.p1_support import (
    FID,
    OTHER_FID,
    PASSWORD,
    Signed,
    app_client,
    password_customer,
    reset_fakes,
    verification,
)
from glowderma.integrations.oauth.registry import fake_apple

pytestmark = pytest.mark.django_db
SOCIAL = "/api/v1/auth/social/"
COMPLETE = "/api/v1/auth/social/complete/"
PHONE = "+998903335501"
APP = "uz.glowderma.app"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def revokes() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(topic="apple.revoke").order_by("pk"))


def link_body(subject: str) -> dict[str, Any]:
    return {
        "provider": "apple",
        "token": f"fake-apple:{subject}:x@privaterelay.appleid.com",
        "nonce": "*",
        "authorization_code": "fake-code:1",
        "password": PASSWORD,
    }


def apple_ticket(subject: str) -> str:
    client = app_client()
    response = client.post(
        SOCIAL,
        {
            "provider": "apple",
            "token": f"fake-apple:{subject}:x@privaterelay.appleid.com",
            "nonce": "*",
            "authorization_code": f"fake-code:{subject}",
        },
    )
    assert response.status_code == 200 and response.data["next_step"] == "verify_phone", response.data
    return str(response.data["social_ticket"])


def complete(ticket: str, phone: str = PHONE) -> Any:
    jti = services.read_social_ticket(ticket, fid=FID).jti
    token = verification(phone, "link_social", jti=jti)
    return app_client().post(COMPLETE, {"social_ticket": ticket, "verification_token": token})


# --- POST me/social/ (the Idempotency-Key transaction rolls back with the refusal)


def test_a_refused_settings_link_through_the_api_keeps_its_revocation() -> None:
    signed = Signed(password_customer("+998903335502"))
    CustomerSocialIdentity.objects.create(customer=signed.customer, provider="apple", subject="already")
    response = signed.client.post("/api/v1/me/social/", link_body("another"), **signed.idem())
    assert (response.status_code, response.data["code"]) == (409, "social_provider_linked")
    assert fake_apple().exchanged == [("fake-code:1", APP)]
    [message] = revokes()
    assert message.payload["identity_id"] is None
    assert message.payload["customer_id"] == signed.customer.pk
    assert message.payload["subject_ref"] == pii_ref("another")
    assert (
        account_services.open_apple_refresh(message.payload["sealed_token"], client_id=APP)
        == "fake-apple-refresh-another"
    )


def test_a_refused_settings_link_of_a_linked_subject_revokes_nothing() -> None:
    """The subject is another account's identity: revoking would end that account's Apple grant."""
    other = password_customer("+998903335503")
    CustomerSocialIdentity.objects.create(customer=other, provider="apple", subject="shared")
    signed = Signed(password_customer("+998903335504"))
    response = signed.client.post("/api/v1/me/social/", link_body("shared"), **signed.idem())
    assert (response.status_code, response.data["code"]) == (409, "social_identity_linked")
    assert revokes() == []


def test_a_successful_settings_link_queues_no_revocation() -> None:
    signed = Signed(password_customer("+998903335505"))
    response = signed.client.post("/api/v1/me/social/", link_body("fresh"), **signed.idem())
    assert response.status_code == 200, response.data
    assert revokes() == []
    assert CustomerSocialIdentity.objects.get(customer=signed.customer).provider_refresh_token


# --- auth/social/complete/


def test_a_final_completion_refusal_revokes_the_waiting_apple_token() -> None:
    customer = Customer.objects.create(phone=PHONE, name="Apple")
    CustomerSocialIdentityFactory.create(customer=customer, provider="apple", subject="a-old")
    start_test_session(customer, fid=OTHER_FID)
    ticket = apple_ticket("a-new")
    jti = services.read_social_ticket(ticket, fid=FID).jti
    response = complete(ticket)
    assert (response.status_code, response.data["code"]) == (409, "social_provider_linked")
    [message] = revokes()
    assert message.payload["customer_id"] == customer.pk
    assert message.payload["subject_ref"] == pii_ref("a-new")
    assert cache.get(services.SOCIAL_APPLE_KEY.format(jti=jti)) is None
    # the dropped token cannot be linked by a retry of the same ticket
    assert complete(ticket).data["code"] == "social_ticket_invalid"


def test_a_completion_refused_for_a_linked_subject_revokes_nothing() -> None:
    Customer.objects.create(phone=PHONE, name="Apple")
    ticket = apple_ticket("a-taken")
    other = Customer.objects.create(phone="+998903335506", name="Other")
    CustomerSocialIdentityFactory.create(customer=other, provider="apple", subject="a-taken")
    response = complete(ticket)
    assert response.data["code"] == "social_identity_linked"
    assert revokes() == []


def test_a_retryable_completion_refusal_keeps_the_token_for_the_retry() -> None:
    password_customer(PHONE)
    ticket = apple_ticket("a-retry")
    response = complete(ticket)  # the account has a password: the client retries with it
    assert response.status_code == 400
    assert revokes() == []
    jti = services.read_social_ticket(ticket, fid=FID).jti
    assert cache.get(services.SOCIAL_APPLE_KEY.format(jti=jti)) is not None


# --- the linked-identity sign-in keeps the stored grant


def test_a_token_exchanged_while_another_sign_in_stored_one_is_dropped_not_revoked() -> None:
    customer = Customer.objects.create(phone=PHONE, name="Apple")
    stored = account_services.seal_apple_refresh("stored", client_id=APP)
    identity = CustomerSocialIdentityFactory.create(
        customer=customer, provider="apple", subject="a-same", provider_refresh_token=stored
    )
    late = account_services.seal_apple_refresh("late", client_id=APP)
    with transaction.atomic():
        assert account_services.touch_identity_sign_in(
            identity.pk, customer.pk, sealed_token=late, client_id=APP
        )
    identity.refresh_from_db()
    assert identity.provider_refresh_token == stored
    assert revokes() == []


# --- the handler and the payload reference


def test_the_handler_skips_a_grant_whose_subject_is_linked_again() -> None:
    sealed = account_services.seal_apple_refresh("old-grant", client_id=APP)
    with transaction.atomic():
        assert account_services.enqueue_apple_token_revoke(
            sealed, client_id=APP, customer_id=7, subject="relinked"
        )
    [message] = revokes()
    customer = Customer.objects.create(phone=PHONE, name="Apple")
    CustomerSocialIdentityFactory.create(customer=customer, provider="apple", subject="relinked")
    tasks.revoke_apple_token(message)
    assert fake_apple().revoked == []
    message.refresh_from_db()
    assert (message.payload["sealed_token"], message.payload["subject"]) == ("", "")
    assert message.payload["subject_ref"] == pii_ref("relinked") and message.payload["customer_id"] == 7


def test_an_unlinked_identity_revocation_carries_its_reference_and_is_blanked_after() -> None:
    customer = Customer.objects.create(phone=PHONE, name="Apple")
    identity = CustomerSocialIdentityFactory.create(
        customer=customer,
        provider="apple",
        subject="a-unlink",
        provider_refresh_token=account_services.seal_apple_refresh("r", client_id=APP),
        provider_client_id=APP,
    )
    with transaction.atomic():
        assert account_services.enqueue_apple_revoke(identity)
        CustomerSocialIdentity.objects.filter(pk=identity.pk).delete()
    [message] = revokes()
    assert message.payload["subject"] == "a-unlink" and message.payload["customer_id"] == customer.pk
    tasks.revoke_apple_token(message)
    assert fake_apple().revoked == [("r", APP)]
    message.refresh_from_db()
    assert (message.payload["sealed_token"], message.payload["subject"]) == ("", "")


def test_the_dead_alert_names_the_grant(caplog: pytest.LogCaptureFixture) -> None:
    message = outbox.enqueue(
        "apple.revoke",
        {
            "identity_id": None,
            **account_services.apple_grant_reference(41, "a-dead"),
            "client_id": APP,
            "sealed_token": "sealed",
        },
        dedupe_key="apple.revoke:token:dead",
    )
    with caplog.at_level(logging.ERROR):
        tasks.apple_revoke_dead(message)
    text = " ".join(record.getMessage() for record in caplog.records)
    assert f"apple.revoke_dead identity_id=None customer_id=41 subject_ref={pii_ref('a-dead')}" in text
    assert "a-dead " not in text and "sealed" not in text
