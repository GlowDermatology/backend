# ruff: noqa: S105, S106, F811 - test credentials; the journey fixtures are imported and requested by name
"""``feat/customer-web``: the seams between B-W1 (the website as a client through its BFF, several sessions at
once, the «Faol seanslar» list) and B-W2 (hybrid identity: e-mail accounts, phoneless accounts, the phone
gate). Each branch tested its own feature; these tests cover what only exists once both are in: the hybrid
journeys driven by the website, account-security notices under the multi-session push rules, an e-mail reset
under the multi-session sign-out rules, deletion scrubbing both the e-mail and the session facts, the admin
session list of a phoneless customer, and the phone gate in front of the website's pay return page."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.commerce.models import Order
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.customers.tests import test_hybrid_identity as hybrid
from glowderma.customers.tests.test_hybrid_identity import (  # noqa: F401 - fixtures requested by name
    CODE,
    attach_phone,
    clean_email,
    email_sign_up,
    verify_email,
)
from glowderma.customers.tests.test_m2_journeys import (  # noqa: F401 - fixtures requested by name
    PASSWORD,
    App,
    Commit,
    World,
    add_address,
    add_to_cart,
    checkout,
    clean_fakes,
    clock,
    commit,
    error,
    fcm,
    pay,
    pushes,
    register_push,
    sign_up,
    stocked,
    verify,
    world,
)
from glowderma.customers.tests.test_web_sessions_api import CHROME_WINDOWS, geo  # noqa: F401 - autouse
from glowderma.integrations.email.registry import fake_email

pytestmark = pytest.mark.django_db

TASHKENT_IP = "84.54.70.10"  # the stub GeoIP database of test_web_sessions_api: "Tashkent, Tashkent, UZ"
SAMARKAND_IP = "185.139.137.1"  # "Samarkand, UZ"


class WebApp(App):
    """One browser on the website: the BFF calls with ``X-GD-Platform: web`` and the internal secret, and
    forwards the browser's IP and user agent (``X-GD-Client-IP`` / ``X-GD-Client-UA``); no app version."""

    def __init__(self, commit: Commit, fid: str, *, lang: str | None = None, ip: str = TASHKENT_IP) -> None:
        super().__init__(commit, fid, platform="web", lang=lang)
        self.ip = ip

    def headers(self, *, auth: bool, idem: str | None, extra: dict[str, Any] | None) -> dict[str, Any]:
        headers = super().headers(auth=auth, idem=idem, extra=extra)
        headers.pop("HTTP_X_GD_APP_VERSION", None)
        return {
            "HTTP_X_GD_INTERNAL_SECRET": settings.INTERNAL_API_SECRET,
            "HTTP_X_GD_CLIENT_IP": self.ip,
            "HTTP_X_GD_CLIENT_UA": CHROME_WINDOWS,
            **headers,
        }


def browser(world: World, fid: str, **kwargs: Any) -> WebApp:
    return WebApp(world.commit, fid, **kwargs)


@pytest.fixture
def web_world(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    """The same world, every ``world.app(...)`` a browser on the website instead of an install."""

    def app(self: World, fid: str, *, platform: str = "web", lang: str | None = None) -> App:
        return WebApp(self.commit, fid, lang=lang)

    monkeypatch.setattr(World, "app", app)
    return world


def email_login_new_device(app: App, email: str, password: str = PASSWORD) -> dict[str, Any]:
    """Password sign-in by e-mail on an unknown device: ``verify_device`` → the e-mail code → tokens."""
    login = {"email": email, "password": password}
    step = app.post("auth/login/", login, auth=False)
    assert step.status_code == 200 and step.json()["next_step"] == "verify_device", step.content
    token = verify_email(app, email, "login_new_device")
    signed = app.post("auth/login/", {**login, "verification_token": token}, auth=False)
    assert signed.status_code == 200, signed.content
    app.store(signed.json())
    return dict(signed.json())


def phone_login_new_device(app: App, phone: str) -> dict[str, Any]:
    login = {"phone": phone, "password": PASSWORD}
    step = app.post("auth/login/", login, auth=False)
    assert step.status_code == 200 and step.json()["next_step"] == "verify_device", step.content
    token = verify(app, phone, "login_new_device")
    signed = app.post("auth/login/", {**login, "verification_token": token}, auth=False)
    assert signed.status_code == 200, signed.content
    app.store(signed.json())
    return dict(signed.json())


# --- the B-W2 journeys, driven by the website ---------------------------------------------------------------

HYBRID_JOURNEYS: list[Callable[[World], None]] = [
    hybrid.test_email_sign_up_cart_checkout_asks_phone_then_attach_then_checkout_and_pay,
    hybrid.test_phone_taken_is_never_merged_sign_in_with_that_phone_instead,
    hybrid.test_social_phoneless_sign_up_booking_asks_phone_then_books,
    hybrid.test_reset_by_email_and_email_sign_in_on_a_new_device,
    hybrid.test_attach_always_reauthenticates_an_account_that_has_a_way_in,
    hybrid.test_a_social_sign_in_alone_cannot_add_a_way_in_to_a_password_account,
    hybrid.test_attach_and_replace_email_then_deletion_scrubs_it,
    hybrid.test_identifier_validation_and_purposes,
]


@pytest.mark.parametrize(
    "journey", HYBRID_JOURNEYS, ids=[journey.__name__.removeprefix("test_") for journey in HYBRID_JOURNEYS]
)
def test_hybrid_journey_on_the_website(journey: Callable[[World], None], web_world: World) -> None:
    """Every hybrid identity journey passes unchanged with ``X-GD-Platform: web`` and the internal secret;
    every session it opened (the validation journey opens none) is a website session that recorded the
    forwarded browser."""
    journey(web_world)
    sessions = AuthSession.objects.filter(audience="customer")
    assert set(sessions.values_list("device__platform", flat=True)) <= {"web"}
    assert set(sessions.exclude(user_agent="").values_list("user_agent", flat=True)) <= {CHROME_WINDOWS}
    assert not sessions.exclude(last_ip__isnull=True).exclude(last_ip=TASHKENT_IP).exists()


# --- account-security notices under the multi-session push rules --------------------------------------------


def test_attach_notices_reach_every_signed_in_device_and_no_signed_out_one(world: World) -> None:
    """B-W2's ``phone_attached`` / ``email_attached`` go through B-W1's fan-out: every device with an open
    session gets the notice (≤10), a device whose session ended gets nothing, the website gets the inbox
    row."""
    web = browser(world, "web-cw-attach-0001")
    email_sign_up(web, "shahzoda@example.uz")
    ios = world.app("fid-cw-attach-ios")
    email_login_new_device(ios, "shahzoda@example.uz")
    register_push(ios, "fcm-cw-attach-ios")
    android = world.app("fid-cw-attach-android", platform="android")
    email_login_new_device(android, "shahzoda@example.uz")
    register_push(android, "fcm-cw-attach-android")
    assert android.post("auth/logout/", {"refresh": android.refresh}, auth=False).status_code == 204
    assert Device.objects.get(fid=android.fid).is_active is False

    attached = attach_phone(web, "+998901250001", password=PASSWORD)
    assert attached.status_code == 200, attached.content
    notices = [m for m in pushes(world, "account_security") if m.data.get("event") == "phone_attached"]
    assert [m.token for m in notices] == ["fcm-cw-attach-ios"]
    assert any(row["title"] for row in web.get("me/notifications/").json()["results"])

    # the iOS session ends too: an e-mail replacement now pushes to nobody, and still leaves the inbox row
    assert ios.post("auth/logout/", {"refresh": ios.refresh}, auth=False).status_code == 204
    world.fcm.sent.clear()
    assert web.post("me/email/code/", {"email": "shahzoda.new@example.uz"}).status_code == 200
    replaced = web.post("me/email/", {"email": "shahzoda.new@example.uz", "code": CODE, "password": PASSWORD})
    assert replaced.status_code == 200, replaced.content
    assert pushes(world, "account_security") == []
    assert web.get("me/notifications/").json()["count"] == 2


# --- an e-mail reset under the multi-session sign-out rules -------------------------------------------------


def test_email_reset_signs_out_every_other_session_and_says_goodbye_to_the_apps(world: World) -> None:
    """A password set through ``auth/reset-password/`` with an e-mail code ends every session of the account —
    other browsers and the apps (401 ``password_changed``) — keeps only the resetting browser signed in, and
    sends the farewell notice to the app it signed out (B-W1 rules, B-W2 identifier)."""
    first = browser(world, "web-cw-reset-0001")
    email_sign_up(first, "mohira@example.uz")
    other = browser(world, "web-cw-reset-0002", ip=SAMARKAND_IP)
    email_login_new_device(other, "mohira@example.uz")
    ios = world.app("fid-cw-reset-ios")
    email_login_new_device(ios, "mohira@example.uz")
    register_push(ios, "fcm-cw-reset-ios")
    assert len(first.get("me/sessions/").json()["results"]) == 3

    resetting = browser(world, "web-cw-reset-0003")
    sent = resetting.post(
        "auth/send-code/", {"email": "mohira@example.uz", "purpose": "reset_password"}, auth=False
    )
    assert sent.status_code == 200, sent.content
    token = verify_email(resetting, "mohira@example.uz", "reset_password")
    new_password = "Quiet!Harbour-Lamp42"
    reset = resetting.post(
        "auth/reset-password/", {"verification_token": token, "password": new_password}, auth=False
    )
    assert reset.status_code == 200, reset.content
    resetting.store(reset.json())

    for signed_out in (first, other, ios):
        error(signed_out.get("me/"), 401, "password_changed")
    [row] = resetting.get("me/sessions/").json()["results"]
    assert row["current"] is True and row["platform"] == "web" and row["device"] == "Chrome (Windows)"
    farewell = [m for m in pushes(world, "account_security") if m.data.get("event") == "password_reset"]
    assert [m.token for m in farewell] == ["fcm-cw-reset-ios"]
    assert Device.objects.get(fid=ios.fid).is_active is False
    revoked = AuthSession.objects.filter(customer__email="mohira@example.uz", revoked_at__isnull=False)
    assert set(revoked.values_list("revoke_reason", flat=True)) == {"password_changed"}


# --- deletion scrubs the e-mail and the session facts -------------------------------------------------------


def test_deleting_a_web_email_account_scrubs_the_address_and_every_session_fact(world: World) -> None:
    web = browser(world, "web-cw-delete-0001")
    signed = email_sign_up(web, "feruza@example.uz")
    second = browser(world, "web-cw-delete-0002", ip=SAMARKAND_IP)
    email_login_new_device(second, "feruza@example.uz")
    listed = {row["location"] for row in web.get("me/sessions/").json()["results"]}
    assert listed == {"Tashkent, Tashkent, UZ", "Samarkand, UZ"}

    deleted = web.delete("me/", {"password": PASSWORD})
    assert deleted.status_code == 204, deleted.content
    customer = Customer.objects.get(pk=signed["customer"]["id"])
    assert (customer.status, customer.phone, customer.email, customer.email_verified_at) == (
        "deleted",
        None,
        None,
        None,
    )
    sessions = AuthSession.objects.filter(customer_id=customer.pk)
    assert sessions.count() == 2
    assert list(sessions.values_list("user_agent", "last_ip", "location").distinct()) == [("", None, "")]
    assert set(sessions.values_list("revoke_reason", flat=True)) == {"deactivated"}
    error(second.get("me/"), 401, "SESSION_EXPIRED")


def test_a_dormant_reset_blanks_the_previous_holders_session_facts(world: World) -> None:
    """A recycled number recovered by SMS (M2-A16) is reset as at deletion: the previous holder's browser,
    IP and location leave the old session rows too; the new holder's session records its own."""
    phone = "+998901250020"
    previous = browser(world, "web-cw-dormant-0001", ip=SAMARKAND_IP)
    sign_up(previous, phone)
    old_sid = previous.sid
    assert AuthSession.objects.get(pk=old_sid).location == "Samarkand, UZ"
    instant = timeutils.now() - timedelta(days=181)
    Customer.objects.filter(phone=phone).update(last_login_at=instant)
    AuthSession.objects.filter(customer__phone=phone).update(last_used_at=instant, created_at=instant)

    holder = browser(world, "web-cw-dormant-0002")
    sent = holder.post("auth/send-code/", {"phone": phone, "purpose": "reset_password"}, auth=False)
    assert sent.status_code == 200, sent.content
    token = verify(holder, phone, "reset_password")
    reset = holder.post(
        "auth/reset-password/", {"verification_token": token, "password": "Copper!Meadow-Wind71"}, auth=False
    )
    assert reset.status_code == 200, reset.content
    holder.store(reset.json())
    assert AuditEntry.objects.filter(action="customer.history_reset").count() == 1
    old = AuthSession.objects.get(pk=old_sid)
    assert (old.user_agent, old.last_ip, old.location, old.revoke_reason) == (
        "",
        None,
        "",
        "password_changed",
    )
    [row] = holder.get("me/sessions/").json()["results"]
    assert (row["device"], row["location"], row["current"]) == (
        "Chrome (Windows)",
        "Tashkent, Tashkent, UZ",
        True,
    )


# --- the admin session list of a phoneless customer ---------------------------------------------------------


def test_admin_lists_and_revokes_the_sessions_of_a_phoneless_customer(
    world: World, staff_client: APIClient
) -> None:
    web = browser(world, "web-cw-admin-0001")
    signed = email_sign_up(web, "lola@example.uz")
    customer_id = signed["customer"]["id"]
    social = browser(world, "web-cw-admin-0002", ip=SAMARKAND_IP)
    hybrid.social_phoneless(world, social, "sub-cw-admin")

    card = staff_client.get(f"/api/v1/admin/customers/{customer_id}/")
    assert card.status_code == 200 and card.json()["phone"] is None, card.content
    listed = staff_client.get(f"/api/v1/admin/customers/{customer_id}/sessions/")
    assert listed.status_code == 200, listed.content
    [row] = listed.json()["results"]
    assert (row["device"], row["platform"], row["location"]) == (
        "Chrome (Windows)",
        "web",
        "Tashkent, Tashkent, UZ",
    )

    social_id = Customer.objects.get(social_identities__subject="sub-cw-admin").pk
    [social_row] = staff_client.get(f"/api/v1/admin/customers/{social_id}/sessions/").json()["results"]
    assert social_row["location"] == "Samarkand, UZ"

    revoked = staff_client.delete(f"/api/v1/admin/customers/{customer_id}/sessions/{row['id']}/")
    assert revoked.status_code == 204, revoked.content
    error(web.get("me/"), 401, "SESSION_EXPIRED")
    entry = AuditEntry.objects.get(action="customer.session_revoked", entity_id=str(customer_id))
    assert entry.actor_type == "staff"
    assert social.get("me/").status_code == 200  # another phoneless customer is untouched


# --- the phone gate in front of the website's checkout, pay return page and QR ------------------------------


def test_phone_gate_then_web_checkout_pay_returns_to_the_website_and_the_qr_is_session_bound(
    world: World, pos_client: APIClient
) -> None:
    web = browser(world, "web-cw-pay-0001")
    email_sign_up(web, "nilufar@example.uz")
    web.lang = "ru"  # the Russian pages of the website: the return page keeps the locale
    serum = stocked(250_000, 5)
    add_to_cart(web, serum, 1)
    address_id = add_address(web)
    body = {
        "fulfilment": "delivery",
        "address_id": address_id,
        "payment_provider": "click",
        "cashback_to_use": 0,
    }
    error(web.post("orders/checkout/", {**body, "expected_total": 0}, idem=True), 409, "phone_required")
    error(web.post("orders/1/pay/", {"provider": None}, idem=True), 409, "phone_required")
    error(web.get("me/qr/"), 409, "phone_required")

    assert attach_phone(web, "+998901250010", password=PASSWORD).status_code == 200
    order = checkout(web, provider="click", address_id=address_id)
    assert Order.objects.get(pk=order["id"]).channel == "web"
    link = pay(web, int(order["id"]))
    assert link["return_url"] == f"https://glowderma.uz/ru/buyurtmalar/{order['id']}?paid=1"
    assert link["app_url"] is None

    qr = web.get("me/qr/")
    assert qr.status_code == 200, qr.content
    resolved = pos_client.post(
        "/api/v1/pos/cashback/resolve-qr/", {"token": qr.json()["token"]}, format="json"
    )
    assert resolved.status_code == 200 and resolved.json()["phone"] == "+998901250010", resolved.content
    # a QR of the browser's session dies with that session
    second = web.get("me/qr/").json()["token"]
    assert web.post("auth/logout/", {"refresh": web.refresh}, auth=False).status_code == 204
    dead = pos_client.post("/api/v1/pos/cashback/resolve-qr/", {"token": second}, format="json")
    error(dead, 409, "qr_invalid")


# --- the e-mail code budget per browser IP behind the BFF ---------------------------------------------------


def test_email_code_ip_cap_counts_the_forwarded_browser_ip_not_the_bff(world: World) -> None:
    """Every website request comes from the BFF's one address: B-W2's per-IP e-mail code cap must count the
    browser's forwarded IP (B-W1 ``core.clientip``), so one visitor cannot lock every other one out."""
    with override_settings(EMAIL_OTP_IP_HOURLY_LIMIT=2):
        tashkent = browser(world, "web-cw-cap-0001")
        for index in range(2):
            sent = tashkent.post(
                "auth/send-code/", {"email": f"cap{index}@example.uz", "purpose": "register"}, auth=False
            )
            assert sent.status_code == 200, sent.content
        error(
            tashkent.post("auth/send-code/", {"email": "cap2@example.uz", "purpose": "register"}, auth=False),
            429,
            "otp_network_limit",
        )
        samarkand = browser(world, "web-cw-cap-0002", ip=SAMARKAND_IP)
        sent = samarkand.post(
            "auth/send-code/", {"email": "cap3@example.uz", "purpose": "register"}, auth=False
        )
        assert sent.status_code == 200, sent.content
    assert {m.to for m in fake_email().sent} == {"cap0@example.uz", "cap1@example.uz", "cap3@example.uz"}


# --- the admin CustomerRef names a phoneless customer -----------------------------------------------------


def test_customer_ref_carries_the_email_of_a_phoneless_customer(world: World) -> None:
    """Orders, payments and appointments render ``CustomerRef``: a phoneless (e-mail) customer is named by
    the address instead of "—"; deletion scrubs it there too."""
    from glowderma.core.api.serializers import CustomerRefSerializer

    web = browser(world, "web-cw-ref-0001")
    signed = email_sign_up(web, "gulnora@example.uz", name="Gulnora")
    customer = Customer.objects.get(pk=signed["customer"]["id"])
    assert CustomerRefSerializer(customer).data == {
        "id": customer.pk,
        "name": "Gulnora",
        "phone": None,
        "email": "gulnora@example.uz",
    }
    assert web.delete("me/", {"password": PASSWORD}).status_code == 204
    customer.refresh_from_db()
    assert CustomerRefSerializer(customer).data["email"] is None
