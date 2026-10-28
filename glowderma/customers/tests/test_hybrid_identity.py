# ruff: noqa: S105, S106, F811 - test credentials; the journey fixtures are imported and requested by name
"""Hybrid identity journeys (customer contract §2.1, §2.2.10): sign-up by e-mail, phoneless social sign-up,
the phone gate (409 ``phone_required``) on checkout, pay, booking and the QR, attaching a phone or an e-mail
(``phone_taken`` / ``email_taken``, never a merge), the re-authentication of an attach, reset by e-mail and
the e-mail sign-in on a new device. The phone flows themselves are the M2 journeys
(``test_m2_journeys.py``), which run unchanged."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from glowderma.accounts.models import AuthSession, Customer, SmsCode
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.customers.tests.test_m2_journeys import (  # noqa: F401 - fixtures requested by name
    PASSWORD,
    TERMS_V1,
    App,
    World,
    add_address,
    add_to_cart,
    book,
    booking_day,
    checkout,
    clean_fakes,
    clock,
    commit,
    error,
    fcm,
    mask,
    pay,
    sign_up,
    slot,
    specialist,
    stocked,
    world,
)
from glowderma.integrations.email.registry import fake_email
from glowderma.integrations.eskiz.registry import fake_eskiz

pytestmark = pytest.mark.django_db
CODE = "123456"  # ESKIZ_FAKE_CODE of the test settings: the fake SMS and the fake e-mail


@pytest.fixture(autouse=True)
def clean_email() -> Iterator[None]:
    fake_email().sent.clear()
    yield
    fake_email().sent.clear()


def verify_email(app: App, email: str, purpose: str) -> str:
    response = app.post(
        "auth/verify-code/",
        {"email": email, "purpose": purpose, "code": settings.ESKIZ_FAKE_CODE},
        auth=False,
    )
    assert response.status_code == 200, response.content
    return str(response.json()["verification_token"])


def email_sign_up(app: App, email: str, *, name: str = "Dilnoza Aliyeva") -> dict[str, Any]:
    """send-code (register, e-mail) → verify-code → set-password: a phoneless account with the address."""
    sent = app.post("auth/send-code/", {"email": email, "purpose": "register"}, auth=False)
    assert sent.status_code == 200, sent.content
    assert sent.json() == {
        "phone_mask": None,
        "email_mask": f"{email[0]}***@{email.split('@')[1]}",
        "purpose": "register",
        "expires_in": 120,
        "resend_in": 60,
        "attempts": 5,
        "length": 6,
    }
    message = [m for m in fake_email().sent if m.to == email][-1]
    assert CODE in message.text and CODE in message.html and message.subject == "Glow Derma: tasdiqlash kodi"
    assert "ro‘yxatdan o‘tish" in message.text and "<!doctype html>" in message.html
    token = verify_email(app, email, "register")
    created = app.post(
        "auth/set-password/",
        {"verification_token": token, "password": PASSWORD, "name": name, "terms_version": TERMS_V1},
        auth=False,
    )
    assert created.status_code == 201, created.content
    body = dict(created.json())
    app.store(body)
    assert body["is_new_customer"] is True and body["device_key"]
    assert body["customer"]["phone"] is None and body["customer"]["email"] == email
    return body


def attach_phone(app: App, phone: str, **reauth: Any) -> Any:
    sent = app.post("me/phone/code/", {"phone": phone})
    assert sent.status_code == 200, sent.content
    assert sent.json()["phone_mask"] == mask(phone) and sent.json()["purpose"] == "attach_phone"
    assert any(CODE in m.text for m in fake_eskiz().sent if m.phone == phone)
    return app.post("me/phone/", {"phone": phone, "code": CODE, **reauth})


def social_phoneless(world: World, app: App, subject: str) -> dict[str, Any]:
    """``auth/social/`` (unknown identity → ``verify_phone``) then ``auth/social/complete/`` without a
    verification token: a new account without a phone."""
    nonce = app.get("auth/social/nonce/", auth=False).json()["nonce"]
    step = app.post(
        "auth/social/",
        {"provider": "google", "token": f"fake:{subject}:{subject}@gmail.com", "nonce": nonce},
        auth=False,
    )
    assert step.status_code == 200 and step.json()["next_step"] == "verify_phone", step.content
    done = app.post(
        "auth/social/complete/",
        {
            "social_ticket": step.json()["social_ticket"],
            "verification_token": None,
            "name": "Kamola Yusupova",
            "terms_version": TERMS_V1,
        },
        auth=False,
    )
    assert done.status_code == 201, done.content
    body = dict(done.json())
    app.store(body)
    assert body["is_new_customer"] is True
    customer = body["customer"]
    assert (customer["phone"], customer["email"], customer["has_password"]) == (None, None, False)
    assert customer["linked_providers"] == ["google"]
    return body


# --- journeys -----------------------------------------------------------------------------------------------


def test_email_sign_up_cart_checkout_asks_phone_then_attach_then_checkout_and_pay(world: World) -> None:
    app = world.app("fid-hybrid-email-1")
    signed = email_sign_up(app, "dilnoza@example.uz")
    assert app.get("me/").json()["email"] == "dilnoza@example.uz"
    serum = stocked(300_000, 5)
    add_to_cart(app, serum, 1)
    address_id = add_address(app)
    quote = app.post("delivery/calculate/", {"fulfilment": "delivery", "address_id": address_id})
    assert quote.status_code == 200, quote.content  # browsing, cart and quote need no phone
    body = {
        "fulfilment": "delivery",
        "address_id": address_id,
        "payment_provider": "click",
        "cashback_to_use": 0,
        "expected_total": quote.json()["expected_total"],
    }
    refused = error(app.post("orders/checkout/", body, idem=True), 409, "phone_required")
    assert (
        refused["message"] == "Davom etish uchun telefon raqamingizni tasdiqlang."
        and refused["details"] is None
    )
    error(app.get("me/qr/"), 409, "phone_required")
    error(app.post("orders/1/pay/", {"provider": None}, idem=True), 409, "phone_required")

    attached = attach_phone(app, "+998901240001", password=PASSWORD)  # a new way in: the password
    assert attached.status_code == 200, attached.content
    assert attached.json()["phone"] == "+998901240001" and attached.json()["email"] == "dilnoza@example.uz"
    assert AuditEntry.objects.filter(
        action="customer.phone_attached", entity_id=str(signed["customer"]["id"])
    )
    error(app.post("me/phone/code/", {"phone": "+998901240002"}), 409, "conflict")  # never replaced

    order = checkout(app, provider="click", address_id=address_id)
    assert order["status"] == "awaiting_payment"
    link = pay(app, int(order["id"]))
    assert link["order_id"] == order["id"]
    assert app.get("me/qr/").status_code == 200


def test_phone_taken_is_never_merged_sign_in_with_that_phone_instead(world: World) -> None:
    owner = world.app("fid-hybrid-owner")
    phone = "+998901240010"
    sign_up(owner, phone)
    other = world.app("fid-hybrid-other")
    email_sign_up(other, "aziza@example.uz")
    # sending the code reveals nothing: who holds the number is answered only after a correct code
    assert other.post("me/phone/code/", {"phone": phone}).status_code == 200
    wrong = other.post("me/phone/", {"phone": phone, "code": "000000", "password": PASSWORD})
    error(wrong, 400, "otp_invalid")
    other.post("me/phone/code/", {"phone": phone})
    taken = error(
        other.post("me/phone/", {"phone": phone, "code": CODE, "password": PASSWORD}), 409, "phone_taken"
    )
    assert taken["message"] == "Bu raqam boshqa hisobga bog‘langan — shu raqam bilan kiring."
    assert taken["details"] == {"deleted": False}
    assert Customer.objects.get(email="aziza@example.uz").phone is None
    # the e-mail of another account is refused the same way, after its code
    assert owner.post("me/email/code/", {"email": "Aziza@Example.uz"}).status_code == 200
    taken = owner.post("me/email/", {"email": "aziza@example.uz", "code": CODE, "password": PASSWORD})
    error(taken, 409, "email_taken")
    # a deleted holder keeps the number (A1): registering with it again reactivates that account
    deleted = Customer.objects.get(phone=phone)
    Customer.objects.filter(pk=deleted.pk).update(status="deleted", deleted_at=deleted.registered_at)
    cache.clear()  # the per-phone throttle (5/min) counted the attempts above
    SmsCode.objects.filter(phone=phone).update(created_at=timezone.now() - timedelta(minutes=2))  # resend
    resent = other.post("me/phone/code/", {"phone": phone})
    assert resent.status_code == 200, resent.content
    gone = error(
        other.post("me/phone/", {"phone": phone, "code": CODE, "password": PASSWORD}), 409, "phone_taken"
    )
    assert gone["details"] == {"deleted": True}
    assert (
        gone["message"] == "Bu raqam o‘chirilgan hisobga tegishli — shu raqam bilan qayta ro‘yxatdan o‘ting."
    )


def test_social_phoneless_sign_up_booking_asks_phone_then_books(world: World) -> None:
    app = world.app("fid-hybrid-social")
    signed = social_phoneless(world, app, "sub-hybrid-1")
    link = specialist(booking_day())
    start = slot(booking_day(), 10)
    error(book(app, link, start), 409, "phone_required")
    attached = attach_phone(app, "+998901240020")
    assert attached.status_code == 200, attached.content
    booked = book(app, link, start)
    assert booked.status_code == 201, booked.content
    assert booked.json()["status"] == "requested"
    customer = Customer.objects.get(pk=signed["customer"]["id"])
    assert customer.phone == "+998901240020" and customer.email is None


def test_reset_by_email_and_email_sign_in_on_a_new_device(world: World) -> None:
    first = world.app("fid-hybrid-reset-1")
    email_sign_up(first, "malika@example.uz")
    error(
        first.post(
            "auth/send-code/", {"email": "nobody@example.uz", "purpose": "reset_password"}, auth=False
        ),
        409,
        "email_not_registered",
    )
    error(
        first.post("auth/send-code/", {"email": "MALIKA@example.uz", "purpose": "register"}, auth=False),
        409,
        "email_registered",
    )
    second = world.app("fid-hybrid-reset-2")
    sent = second.post(
        "auth/send-code/", {"email": "Malika@Example.UZ ", "purpose": "reset_password"}, auth=False
    )
    assert sent.status_code == 200, sent.content
    token = verify_email(second, "malika@example.uz", "reset_password")
    new_password = "Velvet!Orchard-Quiet88"
    reset = second.post(
        "auth/reset-password/", {"verification_token": token, "password": new_password}, auth=False
    )
    assert reset.status_code == 200, reset.content
    second.store(reset.json())
    error(first.get("me/"), 401, "password_changed")

    third = world.app("fid-hybrid-reset-3")
    login = {"email": "malika@example.uz", "password": new_password}
    step = third.post("auth/login/", login, auth=False)
    assert step.status_code == 200, step.content
    assert step.json()["next_step"] == "verify_device"
    assert (step.json()["phone_mask"], step.json()["email_mask"]) == (None, "m***@example.uz")
    assert any("Yangi qurilmadan kirish" in m.text for m in fake_email().sent)
    device_token = verify_email(third, "malika@example.uz", "login_new_device")
    signed = third.post("auth/login/", {**login, "verification_token": device_token}, auth=False)
    assert signed.status_code == 200 and signed.json()["device_key"], signed.content
    wrong = error(
        third.post("auth/login/", {**login, "password": "nope"}, auth=False), 401, "invalid_credentials"
    )
    assert wrong["message"] == "Telefon raqami, email yoki parol noto‘g‘ri."


def test_attach_always_reauthenticates_an_account_that_has_a_way_in(world: World) -> None:
    app = world.app("fid-hybrid-reauth")
    email_sign_up(app, "nodira@example.uz")  # a fresh session: freshness waives nothing here
    phone = "+998901240030"
    refused = attach_phone(app, phone)
    body = error(refused, 400, "validation_error")
    assert body["errors"][0]["field"] == "password" and body["errors"][0]["code"] == "required"
    wrong = app.post("me/phone/", {"phone": phone, "code": CODE, "password": "Wrong-pass-123"})
    assert error(wrong, 400, "validation_error")["errors"][0]["code"] == "password_incorrect"
    # the refusals came before the code check: the code is still usable
    done = app.post("me/phone/", {"phone": phone, "code": CODE, "password": PASSWORD})
    assert done.status_code == 200 and done.json()["phone"] == phone, done.content

    social = world.app("fid-hybrid-reauth-social")
    social_phoneless(world, social, "sub-hybrid-reauth")
    # nothing to re-authenticate with: a fresh social sign-in passes, a stale one must sign in again
    AuthSession.objects.filter(pk=social.sid).update(
        created_at=AuthSession.objects.get(pk=social.sid).created_at - timedelta(minutes=11)
    )
    stale = attach_phone(social, "+998901240031")
    assert error(stale, 409, "reauth_required")["message"] == "Xavfsizlik uchun hisobingizga qaytadan kiring."
    # a password never signs in without a phone or an e-mail: refused, so the account cannot strand itself
    no_way = error(
        social.post("me/password/", {"password": "Amber!Lantern-Deep55"}, idem=True),
        409,
        "phone_or_email_required",
    )
    assert no_way["message"] == "Parol o‘rnatish uchun avval telefon raqami yoki emailni qo‘shing."


def test_a_social_sign_in_alone_cannot_add_a_way_in_to_a_password_account(world: World) -> None:
    """Review B-W2 #1: someone holding only the victim's linked Google identity signs in (no new-device
    step, M2-A10) and must not attach their own e-mail, then reset the password by it."""
    phone = "+998901240060"
    victim = world.app("fid-hybrid-victim")
    sign_up(victim, phone)
    nonce = victim.get("auth/social/nonce/", auth=False).json()["nonce"]
    linked = victim.post(
        "me/social/",
        {"provider": "google", "token": "fake:sub-victim:v@gmail.com", "nonce": nonce, "password": PASSWORD},
        idem=True,
    )
    assert linked.status_code == 200, linked.content
    attacker = world.app("fid-hybrid-attacker")
    nonce = attacker.get("auth/social/nonce/", auth=False).json()["nonce"]
    signed = attacker.post(
        "auth/social/",
        {"provider": "google", "token": "fake:sub-victim:v@gmail.com:x2", "nonce": nonce},
        auth=False,
    )
    assert signed.status_code == 200 and signed.json()["access"], signed.content
    attacker.store(signed.json())
    assert attacker.post("me/email/code/", {"email": "evil@attacker.test"}).status_code == 200
    refused = attacker.post("me/email/", {"email": "evil@attacker.test", "code": CODE})
    assert error(refused, 400, "validation_error")["errors"][0]["field"] == "password"
    for path, body in (
        ("me/phone/", {"phone": "+998901240061", "code": CODE}),
        ("me/password/", {"password": "Attacker-Pass-2026!"}),
    ):
        response = attacker.post(path, body, idem=path == "me/password/")
        assert response.status_code in (400, 409), response.content  # password required / phone already set
    assert Customer.objects.get(phone=phone).email is None
    reset = attacker.post(
        "auth/send-code/", {"email": "evil@attacker.test", "purpose": "reset_password"}, auth=False
    )
    error(reset, 409, "email_not_registered")


def test_attach_and_replace_email_then_deletion_scrubs_it(world: World) -> None:
    app = world.app("fid-hybrid-email-attach")
    phone = "+998901240040"
    signed = sign_up(app, phone)
    assert signed["customer"]["email"] is None
    sent = app.post("me/email/code/", {"email": "Zarina@Example.uz"})
    assert sent.status_code == 200 and sent.json()["email_mask"] == "z***@example.uz", sent.content
    assert any("Emailingizni tasdiqlash" in m.text for m in fake_email().sent)
    wrong = app.post("me/email/", {"email": "zarina@example.uz", "code": "000000", "password": PASSWORD})
    body = error(wrong, 400, "otp_invalid")
    assert body["details"] == {"attempts_left": 4} and body["errors"][0]["field"] == "code"
    needs = app.post("me/email/", {"email": "zarina@example.uz", "code": CODE})
    assert error(needs, 400, "validation_error")["errors"][0]["field"] == "password"
    done = app.post("me/email/", {"email": "zarina@example.uz", "code": CODE, "password": PASSWORD})
    assert done.status_code == 200 and done.json()["email"] == "zarina@example.uz", done.content
    notices = OutboxMessage.objects.filter(topic="push.account_security", payload__event="email_attached")
    assert notices.count() == 1
    # replacing the address notifies the previous one by e-mail (sealed in the payload, blanked once sent)
    cache.clear()  # the per-address throttle (5/min) counted the attempts above
    SmsCode.objects.filter(email="zarina2@example.uz").delete()
    assert app.post("me/email/code/", {"email": "zarina2@example.uz"}).status_code == 200
    replaced = app.post("me/email/", {"email": "zarina2@example.uz", "code": CODE, "password": PASSWORD})
    assert replaced.status_code == 200 and replaced.json()["email"] == "zarina2@example.uz", replaced.content
    [notice] = [m for m in fake_email().sent if m.to == "zarina@example.uz" and "o‘zgartirildi" in m.subject]
    assert "endi hisobga kirish uchun ishlamaydi" in notice.text
    assert OutboxMessage.objects.get(topic="email.notice").payload["to"] == ""
    assert "zarina" not in str(OutboxMessage.objects.get(topic="email.notice").payload)
    SmsCode.objects.filter(email="zarina@example.uz").update(created_at=timezone.now() - timedelta(minutes=2))
    back = app.post("me/email/code/", {"email": "zarina@example.uz"})
    assert back.status_code == 200, back.content
    restored = app.post("me/email/", {"email": "zarina@example.uz", "code": CODE, "password": PASSWORD})
    assert restored.status_code == 200, restored.content
    error(app.post("me/email/code/", {"email": "zarina@example.uz"}), 409, "conflict")
    # e-mail sign-in now works for the phone account too (known device: no code)
    tokens = app.post("auth/login/", {"email": "zarina@example.uz", "password": PASSWORD}, auth=False)
    assert tokens.status_code == 200 and tokens.json()["device_key"] is None, tokens.content
    app.store(tokens.json())

    deleted = app.delete("me/", {"password": PASSWORD})
    assert deleted.status_code == 204, deleted.content
    customer = Customer.objects.get(pk=signed["customer"]["id"])
    assert (customer.status, customer.phone, customer.email, customer.email_verified_at) == (
        "deleted",
        phone,
        None,
        None,
    )
    again = world.app("fid-hybrid-email-again")
    error(
        again.post("auth/login/", {"email": "zarina@example.uz", "password": PASSWORD}, auth=False),
        401,
        "invalid_credentials",
    )
    # the address is free again: an e-mail registration creates a new account (no reactivation by e-mail)
    fresh = email_sign_up(again, "zarina@example.uz")
    assert fresh["customer"]["id"] != customer.pk and fresh["reactivated"] is False


def test_identifier_validation_and_purposes(world: World) -> None:
    app = world.app("fid-hybrid-validation")
    both = app.post(
        "auth/send-code/",
        {"phone": "+998901240050", "email": "a@example.uz", "purpose": "register"},
        auth=False,
    )
    assert error(both, 400, "validation_error")["errors"] == [
        {
            "field": "email",
            "code": "invalid",
            "message": "Telefon raqami yoki emaildan faqat bittasini yuboring.",
        }
    ]
    neither = error(app.post("auth/send-code/", {"purpose": "register"}, auth=False), 400, "validation_error")
    assert neither["errors"][0]["field"] == "phone" and neither["errors"][0]["code"] == "required"
    bad = app.post("auth/send-code/", {"email": "not-an-address", "purpose": "register"}, auth=False)
    assert error(bad, 400, "validation_error")["errors"][0]["message"] == "Email manzilini to‘g‘ri kiriting."
    for purpose in ("verify_email", "attach_phone", "login_new_device"):
        refused = app.post("auth/send-code/", {"email": "a@example.uz", "purpose": purpose}, auth=False)
        assert error(refused, 400, "validation_error")["errors"][0]["code"] == "invalid_choice"
    refused = app.post(
        "auth/verify-code/", {"phone": "+998901240050", "purpose": "attach_phone", "code": CODE}, auth=False
    )
    assert error(refused, 400, "validation_error")["errors"][0]["code"] == "invalid_choice"

    app.post("auth/send-code/", {"email": "farrux.karimov@example.uz", "purpose": "register"}, auth=False)
    token = verify_email(app, "farrux.karimov@example.uz", "register")
    similar = app.post(
        "auth/set-password/",
        {
            "verification_token": token,
            "password": "farrux.karimov1",
            "name": "Farrux",
            "terms_version": TERMS_V1,
        },
        auth=False,
    )
    errors = error(similar, 400, "validation_error")["errors"]
    assert {
        "field": "password",
        "code": "password_too_similar",
        "message": "Parol emailingizga juda o‘xshash.",
    } in (errors)
    row = SmsCode.objects.get(email="farrux.karimov@example.uz")
    assert row.phone == "" and row.purpose == "signup" and CODE not in row.code_hmac
