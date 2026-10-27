# ruff: noqa: S105, S106 - test credentials and token-type literals
"""``me/*`` (customer contract §2.2): profile, consent, image, device, password, social identities, addresses
and ``DELETE me/``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts import topics
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.accounts.tests.factories import (
    AddressFactory,
    CustomerSocialIdentityFactory,
)
from glowderma.commerce.tests.factories import CartFactory, CartItemFactory, OrderFactory
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.customers.tests.p1_support import (
    FID,
    NEW_PASSWORD,
    OTHER_FID,
    PASSWORD,
    TERMS,
    Signed,
    app_client,
    password_customer,
    publish_terms,
    reset_fakes,
    verification,
)


def device_id(started: Any) -> int:
    value = started.session.device_id
    assert value is not None
    return int(value)


pytestmark = pytest.mark.django_db
ME = "/api/v1/me/"
PHONE = "+998905556677"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


@pytest.fixture
def signed() -> Signed:
    return Signed(password_customer(PHONE))


@pytest.fixture
def social_only() -> Signed:
    customer = Customer.objects.create(phone="+998905556678", name="Social")
    CustomerSocialIdentityFactory.create(customer=customer, provider="google", subject="g-1")
    return Signed(customer)


def test_me_shape(signed: Signed) -> None:
    publish_terms()
    AddressFactory.create(customer=signed.customer, is_default=True, text="Toshkent, Yunusobod 4")
    response = signed.client.get(ME)
    assert response.status_code == 200, response.data
    data = response.data
    assert set(data) == {
        "id",
        "phone",
        "email",
        "name",
        "language",
        "image_url",
        "registered_at",
        "has_password",
        "linked_providers",
        "push_enabled",
        "consent",
        "default_address",
    }
    assert data["consent"] == {
        "terms_version": None,
        "accepted_at": None,
        "current_version": TERMS,
        "update_required": True,
    }
    assert data["default_address"]["text"] == "Toshkent, Yunusobod 4"
    assert response["Cache-Control"] == "no-store"


def test_patch_me(signed: Signed) -> None:
    response = signed.client.patch(ME, {"name": "  Malika K ", "language": "en"})
    assert (
        response.status_code == 200
        and response.data["name"] == "Malika K"
        and response.data["language"] == "en"
    )
    missing = signed.client.patch(ME, {"push_enabled": True})
    assert missing.data["errors"][0] == {
        "field": "push_enabled",
        "code": "push_token_missing",
        "message": "Allow notifications for the app to turn them on.",  # the language just became en
    }
    Device.objects.filter(pk=device_id(signed.started)).update(fcm_token="fcm-me")
    assert signed.client.patch(ME, {"push_enabled": True}).data["push_enabled"] is True
    short = signed.client.patch(ME, {"name": "A"})
    assert (
        short.data["errors"][0]["message"] == "Enter your name (at least 2 characters)."
    )  # customer language en


def test_consent(signed: Signed) -> None:
    publish_terms()
    outdated = signed.client.post(ME + "consent/", {"terms_version": "2020-01-01"})
    assert outdated.data["errors"][0]["code"] == "terms_outdated"
    response = signed.client.post(ME + "consent/", {"terms_version": TERMS})
    assert response.status_code == 200 and response.data["consent"]["update_required"] is False
    assert AuditEntry.objects.get(action="customer.consent_accepted").after == {"terms_version": TERMS}


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (20, 10), "#c0ffee").save(output, format="PNG")
    return output.getvalue()


def test_image_put_and_delete(signed: Signed) -> None:
    upload = SimpleUploadedFile("me.png", png(), content_type="image/png")
    response = signed.client.put(ME + "image/", {"file": upload}, format="multipart")
    assert response.status_code == 200, response.data
    assert response.data["image_url"].endswith(".png")
    first = Customer.objects.get(pk=signed.customer.pk).image.name
    assert str(first).startswith(f"customers/{signed.customer.pk}/")
    bad = signed.client.put(ME + "image/", {"file": SimpleUploadedFile("x.png", b"nope")}, format="multipart")
    assert bad.data["errors"][0]["code"] == "unsupported_image"
    assert signed.client.put(ME + "image/", {}, format="multipart").data["errors"][0]["code"] == "required"
    assert signed.client.delete(ME + "image/").status_code == 204
    assert signed.client.delete(ME + "image/").status_code == 204
    assert Customer.objects.get(pk=signed.customer.pk).image.name == ""


def test_device_put(signed: Signed) -> None:
    body = {
        "fcm_token": "fcm-new",
        "push_enabled": True,
        "model": "iPhone 15",
        "os": "iOS 18",
        "locale": "uz-UZ",
    }
    response = signed.client.put(ME + "device/", body)
    assert response.status_code == 200, response.data
    assert (
        response.data["fid"] == FID
        and response.data["push_enabled"] is True
        and response.data["model"] == "iPhone 15"
    )
    missing = signed.client.put(ME + "device/", {"fcm_token": "", "push_enabled": True})
    assert missing.data["errors"][0]["code"] == "push_token_missing"


# --- me/password


def test_change_password_keeps_session_and_identities(signed: Signed) -> None:
    CustomerSocialIdentityFactory.create(customer=signed.customer, provider="google")
    old = AuthSession.objects.create(
        audience="customer",
        customer=signed.customer,
        device_label="x",
        refresh_hash="f" * 64,
        absolute_expires_at=timeutils.now() + timedelta(days=1),
    )
    body = {"current_password": PASSWORD, "password": NEW_PASSWORD}
    key = signed.idem()
    response = signed.client.post(ME + "password/", body, **key)
    assert response.status_code == 200, response.data
    replay = signed.client.post(ME + "password/", body, **key)
    assert replay.status_code == 200 and replay["Idempotent-Replayed"] == "true"
    customer = Customer.objects.get(pk=signed.customer.pk)
    assert account_services.check_customer_password(customer, NEW_PASSWORD)
    assert signed.client.get(ME).status_code == 200  # the current session keeps working
    old.refresh_from_db()
    assert old.revoke_reason == "password_changed"
    assert CustomerSocialIdentity.objects.filter(customer=customer).exists()
    security = OutboxMessage.objects.get(topic="push.account_security")
    assert security.payload["event"] == "password_changed" and security.payload["farewell_tokens"] == []
    assert AuditEntry.objects.get(action="customer.password_changed").after == {"first_password": False}
    assert signed.client.post(ME + "password/", body).data["code"] == "idempotency_key_required"


def test_change_password_refusals(signed: Signed) -> None:
    wrong = signed.client.post(
        ME + "password/", {"current_password": "Nope-nope-1", "password": NEW_PASSWORD}, **signed.idem()
    )
    assert wrong.data["errors"][0] == {
        "field": "current_password",
        "code": "password_incorrect",
        "message": "Parol noto‘g‘ri.",
    }
    same = signed.client.post(
        ME + "password/", {"current_password": PASSWORD, "password": PASSWORD}, **signed.idem()
    )
    assert same.data["errors"][0]["code"] == "password_unchanged"
    missing = signed.client.post(ME + "password/", {"password": NEW_PASSWORD}, **signed.idem())
    assert missing.data["errors"][0] == {
        "field": "current_password",
        "code": "required",
        "message": "Majburiy maydon.",
    }


def test_social_only_sets_a_first_password_with_an_sms_token(social_only: Signed) -> None:
    missing = social_only.client.post(ME + "password/", {"password": NEW_PASSWORD}, **social_only.idem())
    assert missing.data["errors"][0]["field"] == "verification_token"
    token = verification(str(social_only.customer.phone), "reset_password")
    response = social_only.client.post(
        ME + "password/", {"verification_token": token, "password": NEW_PASSWORD}, **social_only.idem()
    )
    assert response.status_code == 200 and response.data["has_password"] is True
    assert AuditEntry.objects.get(action="customer.password_changed").after == {"first_password": True}
    used = social_only.client.post(
        ME + "password/",
        {"verification_token": token, "current_password": NEW_PASSWORD, "password": PASSWORD},
        **social_only.idem(),
    )
    assert used.status_code == 200  # now has a password: the current password is what counts


# --- me/social


def test_link_social_from_settings(signed: Signed) -> None:
    body = {"provider": "google", "token": "fake:g-9:a@gmail.com", "nonce": "*", "password": PASSWORD}
    wrong = signed.client.post(ME + "social/", {**body, "password": "Nope-nope-1"}, **signed.idem())
    assert wrong.data["errors"][0]["code"] == "password_incorrect"
    response = signed.client.post(ME + "social/", body, **signed.idem())
    assert response.status_code == 200, response.data
    assert response.data["linked_providers"] == ["google"]
    assert AuditEntry.objects.get(action="customer.social_linked").after == {
        "provider": "google",
        "via": "settings",
    }
    security = OutboxMessage.objects.get(topic="push.account_security")
    assert security.payload["event"] == "social_linked" and security.payload["farewell_tokens"] == []
    again = signed.client.post(ME + "social/", {**body, "token": "fake:g-10:a@gmail.com"}, **signed.idem())
    assert again.data["code"] == "social_provider_linked"


def test_social_only_link_needs_an_sms_token(social_only: Signed) -> None:
    body = {"provider": "facebook", "token": "fake:fb-1:a@x.uz"}
    missing = social_only.client.post(ME + "social/", body, **social_only.idem())
    assert missing.data["errors"][0] == {
        "field": "verification_token",
        "code": "required",
        "message": "Majburiy maydon.",
    }
    foreign = verification("+998900000001", "reset_password")
    assert (
        social_only.client.post(
            ME + "social/", {**body, "verification_token": foreign}, **social_only.idem()
        ).data["code"]
        == "verification_invalid"
    )
    token = verification(str(social_only.customer.phone), "reset_password")
    ok = social_only.client.post(ME + "social/", {**body, "verification_token": token}, **social_only.idem())
    assert ok.status_code == 200, ok.data
    used = social_only.client.post(
        ME + "social/",
        {**body, "token": "fake:fb-2:a@x.uz", "verification_token": token},
        **social_only.idem(),
    )
    assert used.data["code"] == "verification_invalid"


def test_unlink_social(social_only: Signed) -> None:
    token = verification(str(social_only.customer.phone), "reset_password")
    last = social_only.client.delete(ME + "social/google/", {"verification_token": token})
    assert (last.status_code, last.data["code"]) == (409, "last_sign_in_method")
    assert last.data["details"] == {"provider": "google"}
    apple = CustomerSocialIdentityFactory.create(
        customer=social_only.customer,
        provider="apple",
        provider_refresh_token=account_services.seal_apple_refresh("r", client_id="uz.glowderma.beta"),
        provider_client_id="uz.glowderma.beta",
    )
    token = verification(str(social_only.customer.phone), "reset_password")
    response = social_only.client.delete(ME + "social/apple/", {"verification_token": token})
    assert response.status_code == 200 and response.data["linked_providers"] == ["google"]
    revoke = OutboxMessage.objects.get(topic="apple.revoke")
    assert revoke.payload["identity_id"] == apple.pk and revoke.payload["client_id"] == "uz.glowderma.beta"
    assert AuditEntry.objects.get(action="customer.social_unlinked").after == {
        "provider": "apple",
        "reason": "customer",
    }
    noop_token = verification(str(social_only.customer.phone), "reset_password")
    assert (
        social_only.client.delete(ME + "social/facebook/", {"verification_token": noop_token}).status_code
        == 200
    )
    missing = social_only.client.delete(ME + "social/facebook/", {})
    assert missing.data["errors"][0]["field"] == "verification_token"  # re-authentication before the no-op
    assert social_only.client.delete(ME + "social/twitter/", {}).status_code == 404


# --- addresses


def test_addresses_crud(signed: Signed) -> None:
    key = signed.idem()
    body = {"label": "Uy", "text": " Toshkent, Chilonzor 9 ", "latitude": 41.2854311, "longitude": 69.2035}
    created = signed.client.post(ME + "addresses/", body, **key)
    assert created.status_code == 201, created.data
    assert created.data["is_default"] is True and created.data["latitude"] == 41.285431
    assert signed.client.post(ME + "addresses/", body, **key).data == created.data  # replayed
    assert Address.objects.count() == 1
    second = signed.client.post(ME + "addresses/", {"text": "Toshkent, Sergeli 3"}, **signed.idem()).data
    listed = signed.client.get(ME + "addresses/").data["results"]
    assert [row["id"] for row in listed] == [created.data["id"], second["id"]]
    made_default = signed.client.post(ME + f"addresses/{second['id']}/default/")
    assert made_default.data["is_default"] is True
    assert Customer.objects.get(pk=signed.customer.pk).address_text == "Toshkent, Sergeli 3"
    patched = signed.client.patch(ME + f"addresses/{second['id']}/", {"comment": "3-podyezd"})
    assert patched.data["comment"] == "3-podyezd" and patched.data["text"] == "Toshkent, Sergeli 3"
    assert signed.client.delete(ME + f"addresses/{second['id']}/").status_code == 204
    other = AddressFactory.create()
    assert signed.client.patch(ME + f"addresses/{other.pk}/", {"label": "x"}).status_code == 404
    assert signed.client.delete(ME + f"addresses/{other.pk}/").status_code == 404
    assert signed.client.post(ME + "addresses/", {"text": "x"}).data["code"] == "idempotency_key_required"


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"text": "abc"}, "text", "min_length"),
        ({"text": "Toshkent 1", "region_code": "1a"}, "region_code", "invalid"),
        ({"text": "Toshkent 1", "latitude": 41.0}, "latitude", "latlng_pair"),
        ({"text": "Toshkent 1", "latitude": 91, "longitude": 1}, "latitude", "max_value"),
        ({"text": "Toshkent 1", "label": "x" * 61}, "label", "max_length"),
    ],
)
def test_address_validation(signed: Signed, body: dict[str, Any], field: str, code: str) -> None:
    response = signed.client.post(ME + "addresses/", body, **signed.idem())
    assert response.status_code == 400
    assert (response.data["errors"][0]["field"], response.data["errors"][0]["code"]) == (field, code)


@override_settings(ADDRESS_MAX_PER_CUSTOMER=2)
def test_address_limit(signed: Signed) -> None:
    for index in range(2):
        signed.client.post(ME + "addresses/", {"text": f"Toshkent {index}0"}, **signed.idem())
    response = signed.client.post(ME + "addresses/", {"text": "Toshkent 30"}, **signed.idem())
    assert (response.status_code, response.data["code"]) == (409, "address_limit")
    assert response.data["details"] == {"limit": 2}
    assert response.data["message"] == "Ko‘pi bilan 2 ta manzil saqlash mumkin."


# --- DELETE me/


def test_delete_me_scrubs_and_revokes(signed: Signed) -> None:
    AddressFactory.create(customer=signed.customer)
    CartItemFactory.create(cart=CartFactory.create(customer=signed.customer))
    CustomerSocialIdentityFactory.create(customer=signed.customer, provider="google")
    wrong = signed.client.delete(ME, {"password": "Nope-nope-1"})
    assert wrong.data["errors"][0]["code"] == "password_incorrect"
    response = signed.client.delete(ME, {"password": PASSWORD})
    assert response.status_code == 204
    customer = Customer.objects.get(pk=signed.customer.pk)
    assert (customer.status, customer.name, customer.password) == ("deleted", "", "")
    assert not Address.objects.filter(customer=customer).exists()
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()
    assert not customer.cart.items.exists()
    assert AuditEntry.objects.get(action="customer.deleted").after == {
        "orders_cancelled": 0,
        "appointments_cancelled": 0,
    }
    assert signed.client.get(ME).data["code"] == "SESSION_EXPIRED"
    # re-registration reactivates the same id
    publish_terms()
    token = verification(PHONE, "register")
    again = app_client(fid=OTHER_FID).post(
        "/api/v1/auth/set-password/",
        {"verification_token": token, "password": NEW_PASSWORD, "name": "New", "terms_version": TERMS},
    )
    assert again.status_code == 201 and again.data["customer"]["id"] == customer.pk


def test_delete_me_social_only_needs_an_sms_token(social_only: Signed) -> None:
    assert social_only.client.delete(ME, {}).data["errors"][0]["field"] == "verification_token"
    token = verification(str(social_only.customer.phone), "reset_password")
    assert social_only.client.delete(ME, {"verification_token": token}).status_code == 204


def test_delete_me_refused_while_a_payment_is_in_flight(
    signed: Signed, monkeypatch: pytest.MonkeyPatch
) -> None:
    from glowderma.commerce import selectors as commerce_selectors

    order = OrderFactory.create(customer=signed.customer)
    monkeypatch.setattr(commerce_selectors, "customer_inflight_order", lambda customer_id: order.pk)
    response = signed.client.delete(ME, {"password": PASSWORD})
    assert (response.status_code, response.data["code"]) == (409, "payment_in_progress")
    assert response.data["details"] == {"provider": "click", "order_id": order.pk}
    assert Customer.objects.get(pk=signed.customer.pk).status == "active"


def test_reauth_guard_and_lockout_dos(signed: Signed) -> None:
    for _ in range(5):
        assert signed.client.delete(ME, {"password": "Nope-nope-1"}).status_code == 400
    assert signed.client.delete(ME, {"password": "Nope-nope-1"}).status_code == 429
    # those failures also counted in the phone's customer_login guard
    login = app_client(fid="fid-other-9999").post(
        "/api/v1/auth/login/", {"phone": PHONE, "password": PASSWORD}
    )
    assert login.status_code == 429


def test_phone_lockout_does_not_block_the_signed_in_owner(signed: Signed) -> None:
    attacker = app_client(fid="fid-attacker-01")
    for _ in range(6):
        attacker.post("/api/v1/auth/login/", {"phone": PHONE, "password": "Nope-nope-1"})
    assert attacker.post("/api/v1/auth/login/", {"phone": PHONE, "password": PASSWORD}).status_code == 429
    password = signed.client.post(
        ME + "password/", {"current_password": PASSWORD, "password": NEW_PASSWORD}, **signed.idem()
    )
    assert password.status_code == 200
    link = signed.client.post(
        ME + "social/",
        {"provider": "google", "token": "fake:g-x:a@gmail.com", "nonce": "*", "password": NEW_PASSWORD},
        **signed.idem(),
    )
    assert link.status_code == 200
    assert signed.client.delete(ME, {"password": NEW_PASSWORD}).status_code == 204


def test_me_requires_customer_auth() -> None:
    response = APIClient().get(ME, format="json")
    assert response.status_code in (400, 401)


def test_account_security_payloads_and_localized_errors(signed: Signed) -> None:
    link = {"provider": "facebook", "token": "fake:fb-5:a@x.uz", "password": PASSWORD}
    assert signed.client.post(ME + "social/", link, **signed.idem()).status_code == 200
    assert signed.client.delete(ME + "social/facebook/", {"password": PASSWORD}).status_code == 200
    payloads = [
        message.payload
        for message in OutboxMessage.objects.filter(topic="push.account_security").order_by("id")
    ]
    assert [payload["event"] for payload in payloads] == ["social_linked", "social_unlinked"]
    for payload in payloads:
        assert set(payload) == set(topics.ACCOUNT_SECURITY_PAYLOAD_KEYS) and len(payload["event_id"]) == 32
        assert payload["provider"] == "facebook" and payload["farewell_tokens"] == []
    wrong = {"current_password": "Nope-nope-1", "password": NEW_PASSWORD}
    ru = signed.client.post(ME + "password/", wrong, HTTP_ACCEPT_LANGUAGE="ru", **signed.idem())
    assert ru.data["errors"][0]["message"] == "Неверный пароль." and ru.data["message"] == "Проверьте поля."
    en = signed.client.post(
        ME + "social/", {**link, "password": "Nope-nope-2"}, HTTP_ACCEPT_LANGUAGE="en", **signed.idem()
    )
    assert en.data["errors"][0]["message"] == "Wrong password."
