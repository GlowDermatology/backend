# ruff: noqa: S105, S106 - test credentials and token-type literals
"""Customer social sign-in: nonce, ``auth/social/``, ``auth/social/complete/`` (customer contract
§2.1.8–§2.1.9), Sign in with Apple and the fake-OAuth posture under the production settings (staff routes
included)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, CustomerSocialIdentity, SmsCode, SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import CustomerSocialIdentityFactory, start_test_session
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.customers import services
from glowderma.customers.tests.p1_support import (
    FID,
    OTHER_FID,
    PASSWORD,
    TERMS,
    app_client,
    password_customer,
    publish_terms,
    reset_fakes,
    verification,
)
from glowderma.integrations.oauth.registry import fake_apple

pytestmark = pytest.mark.django_db
NONCE = "/api/v1/auth/social/nonce/"
SOCIAL = "/api/v1/auth/social/"
COMPLETE = "/api/v1/auth/social/complete/"
SEND = "/api/v1/auth/send-code/"
PHONE = "+998903334455"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def nonce(client: APIClient) -> str:
    response = client.get(NONCE)
    assert response.status_code == 200, response.data
    assert response.data["expires_in"] == 600 and len(response.data["nonce"]) == 43
    return str(response.data["nonce"])


def social(client: APIClient, token: str, provider: str = "google", **body: Any) -> Any:
    payload = {"provider": provider, "token": token, **body}
    default_type = "access_token" if provider == "facebook" else "id_token"
    if "nonce" not in body and payload.get("token_type", default_type) == "id_token":
        payload["nonce"] = nonce(client)
    return client.post(SOCIAL, payload)


def ticket_for(client: APIClient, token: str, provider: str = "google", **body: Any) -> str:
    response = social(client, token, provider, **body)
    assert response.status_code == 200, response.data
    assert response.data["next_step"] == "verify_phone"
    return str(response.data["social_ticket"])


def link_token(ticket: str, phone: str = PHONE) -> str:
    jti = services.read_social_ticket(ticket, fid=FID).jti
    return verification(phone, "link_social", jti=jti)


def complete(client: APIClient, ticket: str, token: str, **body: Any) -> Any:
    return client.post(COMPLETE, {"social_ticket": ticket, "verification_token": token, **body})


def test_nonce_is_single_use_and_bound_to_the_install() -> None:
    client = app_client()
    raw = nonce(client)
    assert account_services.consume_social_nonce(raw, OTHER_FID) is False
    assert account_services.consume_social_nonce(raw, FID) is True
    assert account_services.consume_social_nonce(raw, FID) is False
    used = nonce(client)
    assert social(client, "fake:g-1:a@gmail.com", nonce=used).status_code == 200
    assert social(client, "fake:g-2:a@gmail.com", nonce=used).data["code"] == "social_token_invalid"
    assert social(client, "fake:g-3:a@gmail.com", nonce=None).data["code"] == "social_token_invalid"
    assert social(client, "fake:g-4:a@gmail.com", nonce="unknown").data["code"] == "social_token_invalid"


def test_unknown_identity_gets_a_ticket_then_links_by_sms_to_a_social_only_account(
    django_capture_on_commit_callbacks: Any,
) -> None:
    customer = Customer.objects.create(phone=PHONE, name="Social")
    start_test_session(customer, fid=OTHER_FID)
    client = app_client()
    response = social(client, "fake:g-1:malika@gmail.com")
    assert response.data["provider"] == "google" and response.data["email_mask"] == "m***@gmail.com"
    assert response.data["expires_in"] == 600 and response.data["otp"] is None
    ticket = response.data["social_ticket"]
    # send-code with the ticket remembers its jti for the code
    sent = client.post(SEND, {"phone": PHONE, "purpose": "link_social", "social_ticket": ticket})
    assert sent.status_code == 200, sent.data
    row = SmsCode.objects.get(purpose="social_link")
    assert (
        cache.get(account_services.social_otp_key(row.pk)) == services.read_social_ticket(ticket, fid=FID).jti
    )
    token = link_token(ticket)
    done = complete(client, ticket, token)
    assert done.status_code == 200, done.data
    assert done.data["is_new_customer"] is False and len(done.data["device_key"]) == 43
    assert done.data["customer"]["linked_providers"] == ["google"]
    identity = CustomerSocialIdentity.objects.get(customer=customer)
    assert (identity.provider, identity.subject, identity.email_at_link) == (
        "google",
        "g-1",
        "malika@gmail.com",
    )
    security = OutboxMessage.objects.get(topic="push.account_security")
    assert security.payload["event"] == "social_linked" and security.payload["provider"] == "google"
    assert AuditEntry.objects.get(action="customer.social_linked").after == {"provider": "google"}
    # the identity now signs in without SMS
    signed = social(app_client(), "fake:g-1:malika+2@gmail.com")  # a new token (the replay cache)
    assert signed.status_code == 200 and "access" in signed.data
    assert AuditEntry.objects.filter(action="customer.login_succeeded").latest("id").after == {
        "method": "google"
    }


def test_ticket_binding_and_verification_binding() -> None:
    client = app_client()
    ticket = ticket_for(client, "fake:g-1:a@gmail.com")
    other = app_client(fid=OTHER_FID)
    assert complete(other, ticket, link_token(ticket)).data["code"] == "social_ticket_invalid"
    assert complete(client, "forged", link_token(ticket)).data["code"] == "social_ticket_invalid"
    no_ticket = verification(PHONE, "link_social")
    assert complete(client, ticket, no_ticket).data["code"] == "verification_invalid"
    other_ticket = verification(PHONE, "link_social", jti="another-jti")
    assert complete(client, ticket, other_ticket).data["code"] == "verification_invalid"
    sent = client.post(SEND, {"phone": PHONE, "purpose": "link_social", "social_ticket": "forged"})
    assert sent.data["code"] == "social_ticket_invalid"


def test_completion_requires_the_password_of_a_password_account() -> None:
    password_customer(PHONE)
    client = app_client()
    ticket = ticket_for(client, "fake:g-1:a@gmail.com")
    token = link_token(ticket)
    missing = complete(client, ticket, token)
    assert (
        missing.data["errors"][0]["field"] == "password" and missing.data["errors"][0]["code"] == "required"
    )
    wrong = complete(client, ticket, token, password="Wrong-password-1")
    assert wrong.data["errors"][0] == {
        "field": "password",
        "code": "password_incorrect",
        "message": "Parol noto‘g‘ri.",
    }
    ok = complete(client, ticket, token, password=PASSWORD)
    assert ok.status_code == 200, ok.data


def test_completion_creates_or_reactivates_a_customer() -> None:
    publish_terms()
    client = app_client()
    ticket = ticket_for(client, "fake:g-1:a@gmail.com")
    token = link_token(ticket)
    missing = complete(client, ticket, token)
    assert missing.data["errors"][0]["field"] == "name"
    created = complete(client, ticket, token, name="Dilnoza", terms_version=TERMS)
    assert created.status_code == 201, created.data
    assert created.data["is_new_customer"] is True and created.data["customer"]["has_password"] is False
    customer = Customer.objects.get(phone=PHONE)
    assert customer.terms_version == TERMS and customer.password == ""
    assert {e.action for e in AuditEntry.objects.all()} >= {"customer.social_linked", "customer.registered"}
    assert not OutboxMessage.objects.filter(topic="push.account_security").exists()
    # reactivation of a deleted customer
    account_services.delete_customer(customer.pk)
    ticket = ticket_for(app_client(), "fake:g-9:b@gmail.com")
    again = complete(app_client(), ticket, link_token(ticket), name="Dilnoza", terms_version=TERMS)
    assert again.status_code == 201 and again.data["reactivated"] is True


def test_conflicts() -> None:
    customer = Customer.objects.create(phone=PHONE, name="S")
    CustomerSocialIdentityFactory.create(customer=customer, provider="google", subject="g-old")
    start_test_session(customer, fid=OTHER_FID)
    Customer.objects.filter(pk=customer.pk).update(last_login_at=None)
    customer.refresh_from_db()
    # active (non-dormant: the session's last use counts) and already holding Google
    client = app_client()
    ticket = ticket_for(client, "fake:g-new:a@gmail.com")
    response = complete(client, ticket, link_token(ticket))
    assert (response.status_code, response.data["code"]) == (409, "social_provider_linked")
    assert response.data["details"] == {"provider": "google"}
    assert response.data["message"] == "Bu hisobga boshqa Google profili bog‘langan."
    # the subject belongs to another customer (linked meanwhile)
    other = Customer.objects.create(phone="+998903334456", name="O")
    ticket = ticket_for(client, "fake:fb-1:a@x.uz", "facebook")
    CustomerSocialIdentityFactory.create(customer=other, provider="facebook", subject="fb-1")
    response = complete(client, ticket, link_token(ticket))
    assert response.data["code"] == "social_identity_linked"


def test_token_pairs_and_replay() -> None:
    client = app_client()
    pair = social(client, "fake:g-1:a@gmail.com", token_type="access_token")
    assert (pair.status_code, pair.data["code"]) == (400, "social_token_invalid")
    apple_pair = social(
        client,
        "fake-apple:a-1:x@privaterelay.appleid.com",
        "apple",
        token_type="access_token",
        authorization_code="fake-code:1",
    )
    assert apple_pair.data["code"] == "social_token_invalid"
    assert social(client, "fake:fb-1:a@x.uz", "facebook").status_code == 200  # access token, no nonce
    assert social(client, "fake:fb-2:a@x.uz", "facebook", token_type="id_token").status_code == 200
    first = social(client, "fake:g-7:a@gmail.com")
    assert first.status_code == 200
    replay = social(client, "fake:g-7:a@gmail.com")
    assert replay.data["code"] == "social_token_invalid"
    assert social(client, "fake:g-7:b@gmail.com").status_code == 200  # another token, same subject
    for token, status, code in (
        ("fake-fail", 400, "social_token_invalid"),
        ("fake-expired", 400, "social_token_invalid"),
        ("fake-down", 503, "provider_unavailable"),
    ):
        response = social(client, token)
        assert (response.status_code, response.data["code"]) == (status, code)


def test_unknown_provider_is_404() -> None:
    assert app_client().post(SOCIAL, {"provider": "twitter", "token": "x"}).status_code == 404


def test_apple_sign_in_stores_the_sealed_refresh_token_with_its_client_id() -> None:
    client = app_client()
    missing = social(client, "fake-apple:a-1:x@privaterelay.appleid.com", "apple")
    assert missing.data["errors"][0] == {
        "field": "authorization_code",
        "code": "required",
        "message": "Majburiy maydon.",
    }
    with override_settings(APPLE_BUNDLE_IDS=["uz.glowderma.app", "uz.glowderma.beta"]):
        ticket = ticket_for(
            client,
            "fake-apple:a-1:x@privaterelay.appleid.com:uz.glowderma.beta",
            "apple",
            authorization_code="fake-code:1",
        )
        customer = Customer.objects.create(phone=PHONE, name="Apple")
        done = complete(client, ticket, link_token(ticket))
    assert done.status_code == 200, done.data
    identity = CustomerSocialIdentity.objects.get(customer=customer, provider="apple")
    assert identity.provider_client_id == "uz.glowderma.beta"
    assert (
        account_services.open_apple_refresh(identity.provider_refresh_token, client_id="uz.glowderma.beta")
        == "fake-apple-refresh-a-1"
    )
    assert fake_apple().exchanged == [("fake-code:1", "uz.glowderma.beta")]


def test_apple_refusals_link_nothing() -> None:
    client = app_client()
    refused = social(client, "fake-apple:a-1:x@y.z", "apple", authorization_code="fake-code-fail")
    assert (refused.status_code, refused.data["code"]) == (400, "social_token_invalid")
    down = social(client, "fake-apple:a-2:x@y.z", "apple", authorization_code="fake-code-down")
    assert (down.status_code, down.data["code"]) == (503, "provider_unavailable")
    ticket = ticket_for(client, "fake-apple:a-3:x@y.z", "apple", authorization_code="fake-code:3")
    cache.delete(services.SOCIAL_APPLE_KEY.format(jti=services.read_social_ticket(ticket, fid=FID).jti))
    Customer.objects.create(phone=PHONE, name="A")
    assert complete(client, ticket, link_token(ticket)).data["code"] == "social_ticket_invalid"
    assert not CustomerSocialIdentity.objects.exists()


def test_apple_availability_by_bundle_ids_and_mode() -> None:
    with override_settings(OAUTH_MODE="live", APPLE_BUNDLE_IDS=[]):
        response = app_client().post(SOCIAL, {"provider": "apple", "token": "x", "authorization_code": "c"})
        assert response.status_code == 404
    with override_settings(OAUTH_MODE="fake", APPLE_BUNDLE_IDS=[]):
        assert (
            social(
                app_client(), "fake-apple:a-9:x@y.z", "apple", authorization_code="fake-code:9"
            ).status_code
            == 200
        )


def test_linked_apple_identity_without_token_gets_one_at_sign_in() -> None:
    customer = Customer.objects.create(phone=PHONE, name="A")
    CustomerSocialIdentityFactory.create(customer=customer, provider="apple", subject="a-5")
    response = social(app_client(), "fake-apple:a-5:x@y.z", "apple", authorization_code="fake-code:5")
    assert response.status_code == 200 and "access" in response.data
    identity = CustomerSocialIdentity.objects.get(customer=customer)
    assert identity.provider_client_id == "uz.glowderma.app" and identity.provider_refresh_token


# --- fake OAuth under the production settings (C§6.14)


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_fake_oauth_in_production_is_404_on_the_customer_routes() -> None:
    client = app_client()
    assert client.get(NONCE).status_code == 404
    assert (
        client.post(SOCIAL, {"provider": "google", "token": "fake:1:a@b.c", "nonce": "*"}).status_code == 404
    )
    assert client.post(COMPLETE, {"social_ticket": "x", "verification_token": "y"}).status_code == 404
    assert (
        client.post(SEND, {"phone": PHONE, "purpose": "link_social", "social_ticket": "x"}).status_code == 404
    )


def test_wildcard_nonce_only_under_local_settings() -> None:
    assert social(app_client(), "fake:g-1:a@gmail.com", nonce="*").status_code == 200
    with override_settings(OAUTH_MODE="fake"):
        assert services._nonce_accepted("*", FID) is True
    with override_settings(DEV_FEATURES_ALLOWED=False):
        assert services._nonce_accepted("*", FID) is False


STAFF = "/api/v1/admin/auth/oauth/"


def staff_body(code: str, **extra: Any) -> dict[str, Any]:
    import secrets

    return {
        "code": code,
        "code_verifier": secrets.token_urlsafe(48),
        "redirect_uri": "http://localhost:3000/admin/auth/oauth/google/callback",
        **extra,
    }


def test_staff_oauth_is_404_with_fake_oauth_in_production(
    anon_client: APIClient, staff_client: APIClient, staff: StaffUser
) -> None:
    SocialIdentity.objects.create(staff=staff, provider="google", subject="42", email_at_link=staff.email)
    login = staff_body(f"fake:42:{staff.email}", nonce="nonce-42")
    with override_settings(DEV_FEATURES_ALLOWED=False):
        assert anon_client.post(STAFF + "google/", login).status_code == 404
        assert staff_client.post(STAFF + "facebook/link/", staff_body("fake:fb:a@b.c")).status_code == 404
        assert staff_client.delete(STAFF + "google/").status_code == 204
        password = anon_client.post("/api/v1/admin/auth/login/", {"email": staff.email, "password": PASSWORD})
        assert password.status_code == 200
    assert not AuditEntry.objects.filter(action="auth.login_succeeded", after__method="google").exists()
    SocialIdentity.objects.create(staff=staff, provider="google", subject="42", email_at_link=staff.email)
    assert (
        anon_client.post(
            STAFF + "google/", staff_body(f"fake:42:{staff.email}", nonce="nonce-42")
        ).status_code
        == 200
    )


# --- B-W1: social sign-in from the website ------------------------------------------------------------------

WEB_FID = "web-browser-cookie-0003"
SERVICES_ID = "uz.glowderma.web"
WEB_REDIRECT = "https://glowderma.uz/auth/apple/callback"


def web_client() -> APIClient:
    client = APIClient()
    client.default_format = "json"
    client.credentials(HTTP_X_GD_DEVICE_ID=WEB_FID, HTTP_X_GD_PLATFORM="web")
    return client


@override_settings(APPLE_SERVICES_IDS=[SERVICES_ID], APPLE_WEB_REDIRECT_URI=WEB_REDIRECT)
def test_apple_from_the_website_exchanges_with_the_services_id_and_redirect_uri() -> None:
    client = web_client()
    ticket = ticket_for(
        client, f"fake-apple:w-1:x@y.z:{SERVICES_ID}", "apple", authorization_code="fake-code:w1"
    )
    assert fake_apple().exchanged == [("fake-code:w1", SERVICES_ID)]
    assert fake_apple().redirect_uris == [WEB_REDIRECT]
    customer = Customer.objects.create(phone=PHONE, name="Web")
    jti = services.read_social_ticket(ticket, fid=WEB_FID).jti
    done = complete(client, ticket, verification(PHONE, "link_social", jti=jti))
    assert done.status_code == 200, done.data
    identity = CustomerSocialIdentity.objects.get(customer=customer, provider="apple")
    assert identity.provider_client_id == SERVICES_ID  # the revocation uses the Services ID
    assert done.data["customer"]["id"] == customer.pk


@override_settings(GOOGLE_WEB_CLIENT_IDS=["web-client.apps.googleusercontent.com"])
def test_google_from_the_website_signs_a_linked_identity_in() -> None:
    customer = password_customer(PHONE)
    CustomerSocialIdentityFactory.create(customer=customer, provider="google", subject="g-web")
    response = social(web_client(), "fake:g-web:m@gmail.com", "google")
    assert response.status_code == 200 and "access" in response.data
    session = customer.sessions.get(revoked_at__isnull=True)
    assert session.device is not None and session.device.platform == "web"


def test_a_website_nonce_is_bound_to_the_browser() -> None:
    client = web_client()
    raw = nonce(client)
    other = app_client()
    assert social(other, "fake:g-1:m@gmail.com", "google", nonce=raw).data["code"] == "social_token_invalid"
