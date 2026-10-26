# ruff: noqa: S105, S106 - test credentials
"""Fix pass 1 regressions: blank names answer ``min_length`` (``field.name_min``), a used social ticket is
refused by send-code, reference codes and category slugs answer the contract codes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from glowderma.accounts.models import Customer, SmsCode
from glowderma.catalog.tests.factories import CategoryFactory
from glowderma.customers import services
from glowderma.customers.tests.p1_support import (
    FID,
    PASSWORD,
    TERMS,
    Signed,
    app_client,
    password_customer,
    publish_terms,
    reset_fakes,
    verification,
)

pytestmark = pytest.mark.django_db
SET_PASSWORD = "/api/v1/auth/set-password/"
NONCE = "/api/v1/auth/social/nonce/"
SOCIAL = "/api/v1/auth/social/"
COMPLETE = "/api/v1/auth/social/complete/"
SEND = "/api/v1/auth/send-code/"
ME = "/api/v1/me/"
PHONE = "+998907778899"
NAME_MIN = {"field": "name", "code": "min_length", "message": "Ismingizni kiriting (kamida 2 belgi)."}


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


@pytest.mark.parametrize("name", ["", "   ", " a "])
def test_set_password_blank_or_short_name_answers_name_min(name: str) -> None:
    publish_terms()
    token = verification(PHONE, "register")
    body = {"verification_token": token, "password": PASSWORD, "name": name, "terms_version": TERMS}
    response = app_client().post(SET_PASSWORD, body)
    assert response.status_code == 400
    assert response.data["errors"] == [NAME_MIN]
    assert not Customer.objects.filter(phone=PHONE).exists()


def test_set_password_missing_name_is_still_required() -> None:
    publish_terms()
    token = verification(PHONE, "register")
    body = {"verification_token": token, "password": PASSWORD, "terms_version": TERMS}
    response = app_client().post(SET_PASSWORD, body)
    assert response.data["errors"][0]["field"] == "name"
    assert response.data["errors"][0]["code"] == "required"


@pytest.mark.parametrize("name", ["", "   "])
def test_patch_me_blank_name_answers_name_min(name: str) -> None:
    signed = Signed(password_customer(PHONE))
    response = signed.client.patch(ME, {"name": name})
    assert response.status_code == 400
    assert response.data["errors"] == [NAME_MIN]
    assert Customer.objects.get(phone=PHONE).name == "Malika"


def _ticket(client: Any, subject: str) -> str:
    nonce = client.get(NONCE).data["nonce"]
    response = client.post(
        SOCIAL, {"provider": "google", "token": f"fake:{subject}:x@gmail.com", "nonce": nonce}
    )
    assert response.data["next_step"] == "verify_phone", response.data
    return str(response.data["social_ticket"])


def test_social_complete_blank_name_answers_name_min() -> None:
    publish_terms()
    client = app_client()
    ticket = _ticket(client, "g-blank")
    jti = services.read_social_ticket(ticket, fid=FID).jti
    token = verification(PHONE, "link_social", jti=jti)
    body = {"social_ticket": ticket, "verification_token": token, "name": "  ", "terms_version": TERMS}
    response = client.post(COMPLETE, body)
    assert response.status_code == 400
    assert response.data["errors"] == [NAME_MIN]


def test_send_code_refuses_a_ticket_already_completed() -> None:
    customer = Customer.objects.create(phone=PHONE, name="Social")
    client = app_client()
    ticket = _ticket(client, "g-used")
    token = verification(PHONE, "link_social", jti=services.read_social_ticket(ticket, fid=FID).jti)
    done = client.post(COMPLETE, {"social_ticket": ticket, "verification_token": token})
    assert done.status_code == 200, done.data
    assert customer.social_identities.count() == 1
    sent = client.post(SEND, {"phone": PHONE, "purpose": "link_social", "social_ticket": ticket})
    assert sent.status_code == 400
    assert sent.data["code"] == "social_ticket_invalid"
    assert SmsCode.objects.filter(purpose="social_link").count() == 1  # only the completed one, no new code


@pytest.mark.parametrize("field", ["region_code", "city_code"])
def test_long_reference_codes_answer_invalid(field: str) -> None:
    signed = Signed(password_customer(PHONE))
    body = {"text": "Toshkent, Chilonzor 1", field: "1" * 17}
    response = signed.client.post(ME + "addresses/", body, **signed.idem())
    assert response.status_code == 400
    assert response.data["errors"][0]["field"] == field
    assert response.data["errors"][0]["code"] == "invalid"


def test_long_unknown_category_slug_answers_invalid_choice() -> None:
    CategoryFactory.create()
    response = app_client().get("/api/v1/catalog/products/", {"category": "x" * 80})
    assert response.status_code == 400
    assert response.data["errors"][0] == {
        "field": "category",
        "code": "invalid_choice",
        "message": response.data["errors"][0]["message"],
    }
