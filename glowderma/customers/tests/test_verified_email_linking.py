# ruff: noqa: S105, S106, F811 - test credentials; the journey fixtures are imported and requested by name
"""One mailbox, one account: a Google identity whose provider vouches for its address (``email_verified``
and a Gmail or Google Workspace ``hd`` address) signs in to the account holding that address and is linked
to it; a sign-up through such an identity keeps the address. Untrusted addresses (unverified, other
domains, Facebook) keep the ``verify_phone`` ticket flow."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.accounts.models import Customer, CustomerSocialIdentity
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.test_hybrid_identity import clean_email, email_sign_up  # noqa: F401
from glowderma.customers.tests.test_m2_journeys import (  # noqa: F401 - fixtures requested by name
    PASSWORD,
    TERMS_V1,
    App,
    World,
    clean_fakes,
    clock,
    commit,
    fcm,
    sign_up,
    world,
)

pytestmark = pytest.mark.django_db


def social(app: App, token: str, provider: str = "google") -> Any:
    body: dict[str, Any] = {"provider": provider, "token": token}
    if provider == "google":
        body["nonce"] = app.get("auth/social/nonce/", auth=False).json()["nonce"]
    else:
        body["token_type"] = "access_token"
    return app.post("auth/social/", body, auth=False)


def test_verified_gmail_signs_in_to_the_account_of_that_address_and_links_it(world: World) -> None:
    app = world.app("fid-trusted-1")
    created = email_sign_up(app, "bahodir@gmail.com")
    customer_id = created["customer"]["id"]

    signed = social(app, "fake:sub-trusted-1:Bahodir@Gmail.com")
    assert signed.status_code == 200, signed.content
    body = signed.json()
    assert body["access"] and body["device_key"]
    assert body["customer"]["id"] == customer_id
    assert body["customer"]["email"] == "bahodir@gmail.com"
    assert body["customer"]["linked_providers"] == ["google"]
    assert Customer.objects.count() == 1
    identity = CustomerSocialIdentity.objects.get(provider="google", subject="sub-trusted-1")
    assert identity.customer_id == customer_id and identity.email_at_link == "bahodir@gmail.com"
    assert AuditEntry.objects.filter(
        action="customer.social_linked", entity_id=str(customer_id), after__method="verified_email"
    ).exists()

    # The same browser signs in with the password next: it is a known device (no new-device code).
    app.store(body)
    login = app.post("auth/login/", {"email": "bahodir@gmail.com", "password": PASSWORD}, auth=False)
    assert login.status_code == 200 and login.json().get("access"), login.content
    app.store(login.json())
    assert app.get("me/").json()["linked_providers"] == ["google"]

    # And Google again goes straight in, still one account.
    again = social(app, "fake:sub-trusted-1:bahodir@gmail.com")
    assert again.status_code == 200 and again.json()["customer"]["id"] == customer_id
    assert CustomerSocialIdentity.objects.filter(customer_id=customer_id).count() == 1


def test_workspace_address_with_its_hd_is_trusted(world: World) -> None:
    app = world.app("fid-trusted-hd")
    created = email_sign_up(app, "nodira@clinic.uz")
    signed = social(app, "fake:sub-hd:nodira@clinic.uz:clinic.uz")
    assert signed.status_code == 200 and signed.json()["customer"]["id"] == created["customer"]["id"]


@pytest.mark.parametrize(
    ("token", "provider"),
    [
        pytest.param("fake-unverified:sub-u:bahodir@gmail.com", "google", id="unverified"),
        pytest.param("fake:sub-o:bahodir@clinic.uz", "google", id="other_domain_without_hd"),
        pytest.param("fake:sub-w:bahodir@clinic.uz:other.uz", "google", id="hd_of_another_domain"),
        pytest.param("fake:sub-f:bahodir@gmail.com", "facebook", id="facebook"),
    ],
)
def test_untrusted_addresses_keep_the_ticket_flow(world: World, token: str, provider: str) -> None:
    app = world.app("fid-untrusted")
    email = token.split(":")[2]
    email_sign_up(app, email)
    step = social(app, token, provider)
    assert step.status_code == 200, step.content
    assert step.json()["next_step"] == "verify_phone"
    assert not CustomerSocialIdentity.objects.exists()


def test_an_account_with_another_google_identity_is_not_relinked(world: World) -> None:
    app = world.app("fid-trusted-other")
    email_sign_up(app, "lola@gmail.com")
    assert social(app, "fake:sub-first:lola@gmail.com").json()["access"]
    step = social(app, "fake:sub-second:lola@gmail.com")
    assert step.status_code == 200 and step.json()["next_step"] == "verify_phone", step.content
    assert list(CustomerSocialIdentity.objects.values_list("subject", flat=True)) == ["sub-first"]


def test_a_phone_account_without_the_address_is_not_matched(world: World) -> None:
    app = world.app("fid-trusted-phone")
    sign_up(app, "+998901240070")
    step = social(app, "fake:sub-phone:someone@gmail.com")
    assert step.json()["next_step"] == "verify_phone"


def test_a_trusted_sign_up_keeps_the_address(world: World) -> None:
    app = world.app("fid-trusted-new")
    step = social(app, "fake:sub-new:Aziza@gmail.com")
    assert step.status_code == 200 and step.json()["next_step"] == "verify_phone", step.content
    done = app.post(
        "auth/social/complete/",
        {
            "social_ticket": step.json()["social_ticket"],
            "verification_token": None,
            "name": "Aziza Karimova",
            "terms_version": TERMS_V1,
        },
        auth=False,
    )
    assert done.status_code == 201, done.content
    customer = done.json()["customer"]
    assert (customer["phone"], customer["email"], customer["linked_providers"]) == (
        None,
        "aziza@gmail.com",
        ["google"],
    )
    # Next time the same Google identity signs in directly (it is linked).
    other = world.app("fid-trusted-new-2")
    again = social(other, "fake:sub-new:aziza@gmail.com")
    assert again.status_code == 200 and again.json()["customer"]["id"] == customer["id"]


def test_an_untrusted_sign_up_keeps_no_address(world: World) -> None:
    app = world.app("fid-untrusted-new")
    step = social(app, "fake-unverified:sub-unv:aziza@gmail.com")
    done = app.post(
        "auth/social/complete/",
        {
            "social_ticket": step.json()["social_ticket"],
            "verification_token": None,
            "name": "Aziza Karimova",
            "terms_version": TERMS_V1,
        },
        auth=False,
    )
    assert done.status_code == 201, done.content
    assert done.json()["customer"]["email"] is None
