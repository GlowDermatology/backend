# ruff: noqa: S105, S106, F811 - test credentials; the journey fixtures are imported and requested by name
"""The new-device code's channel (``otp_channel``, ``NextStep.channels``) and the security alert e-mails
(new-device sign-in, password reset, password change) of customer contract §2.1.4 / §2.2."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings

from glowderma.customers.tests.test_hybrid_identity import (  # noqa: F401 - fixtures requested by name
    attach_phone,
    clean_email,
    email_sign_up,
)
from glowderma.customers.tests.test_m2_journeys import (  # noqa: F401 - fixtures requested by name
    PASSWORD,
    App,
    World,
    clean_fakes,
    clock,
    commit,
    error,
    fcm,
    mask,
    sign_up,
    world,
)
from glowderma.integrations.devsms.registry import fake_sms
from glowderma.integrations.email.registry import fake_email

pytestmark = pytest.mark.django_db
EMAIL = "zarina@example.uz"
PHONE = "+998901240077"
ALERT = "Glow Derma: hisobingizga yangi qurilmadan kirildi"


@pytest.fixture(autouse=True)
def clean_sms() -> Iterator[None]:
    fake_sms().sent.clear()
    yield
    fake_sms().sent.clear()


def both_identifiers(world: World) -> App:
    """An account with a verified e-mail (sign-up) and a phone (attached with the password)."""
    app = world.app("fid-channels-home")
    email_sign_up(app, EMAIL)
    attached = attach_phone(app, PHONE, password=PASSWORD)
    assert attached.status_code == 200, attached.content
    fake_email().sent.clear()
    fake_sms().sent.clear()
    return app


def verify(app: App, body: dict[str, Any]) -> Any:
    return app.post(
        "auth/verify-code/",
        {**body, "purpose": "login_new_device", "code": settings.OTP_FAKE_CODE},
        auth=False,
    )


def subjects() -> list[str]:
    return [message.subject for message in fake_email().sent]


def test_a_phone_login_can_send_the_new_device_code_to_the_email(world: World) -> None:
    both_identifiers(world)
    other = world.app("fid-channels-new")
    login = {"phone": PHONE, "password": PASSWORD}
    step = other.post("auth/login/", login, auth=False)
    assert step.status_code == 200, step.content
    body = step.json()
    assert body["next_step"] == "verify_device"
    assert (body["phone_mask"], body["email_mask"]) == (mask(PHONE), None)
    assert body["channels"] == [
        {"channel": "sms", "mask": mask(PHONE)},
        {"channel": "email", "mask": "z***@example.uz"},
    ]
    assert [m.phone for m in fake_sms().sent] == [PHONE]
    switched = other.post("auth/login/", {**login, "otp_channel": "email"}, auth=False)
    assert switched.status_code == 200, switched.content
    assert (switched.json()["phone_mask"], switched.json()["email_mask"]) == (None, "z***@example.uz")
    assert any(m.to == EMAIL and settings.OTP_FAKE_CODE in m.text for m in fake_email().sent)
    # the identifier stays the phone signed in with; the channel says where the code went
    token = verify(other, {"phone": PHONE, "otp_channel": "email"})
    assert token.status_code == 200, token.content
    # the token works whether or not the app repeats otp_channel on the last call
    signed = other.post(
        "auth/login/", {**login, "verification_token": token.json()["verification_token"]}, auth=False
    )
    assert signed.status_code == 200 and signed.json()["device_key"], signed.content
    assert subjects().count(ALERT) == 1  # the alert goes to the verified address, once


def test_an_email_login_can_send_the_code_by_sms(world: World) -> None:
    both_identifiers(world)
    other = world.app("fid-channels-sms")
    login = {"email": EMAIL, "password": PASSWORD, "otp_channel": "sms"}
    step = other.post("auth/login/", login, auth=False)
    assert step.status_code == 200, step.content
    assert (step.json()["phone_mask"], step.json()["email_mask"]) == (mask(PHONE), None)
    assert [m.phone for m in fake_sms().sent] == [PHONE]
    token = verify(other, {"email": EMAIL, "otp_channel": "sms"})
    assert token.status_code == 200, token.content
    signed = other.post(
        "auth/login/", {**login, "verification_token": token.json()["verification_token"]}, auth=False
    )
    assert signed.status_code == 200 and signed.json()["device_key"], signed.content


def test_a_channel_the_account_lacks_is_refused_after_the_password(world: World) -> None:
    sign_up(world.app("fid-channels-phone-only"), PHONE)
    fake_email().sent.clear()
    other = world.app("fid-channels-phone-only-2")
    step = other.post("auth/login/", {"phone": PHONE, "password": PASSWORD}, auth=False)
    assert step.json()["channels"] == [{"channel": "sms", "mask": mask(PHONE)}]
    refused = error(
        other.post("auth/login/", {"phone": PHONE, "password": PASSWORD, "otp_channel": "email"}, auth=False),
        400,
        "validation_error",
    )
    assert refused["errors"][0]["field"] == "otp_channel"
    # a wrong password never tells whether the account has an e-mail
    error(
        other.post(
            "auth/login/", {"phone": PHONE, "password": "Wrong-pass-123", "otp_channel": "email"}, auth=False
        ),
        401,
        "invalid_credentials",
    )
    assert fake_email().sent == []


def test_otp_channel_belongs_to_the_new_device_code_only(world: World) -> None:
    app = world.app("fid-channels-purpose")
    refused = error(
        app.post(
            "auth/verify-code/",
            {"phone": PHONE, "purpose": "register", "code": settings.OTP_FAKE_CODE, "otp_channel": "email"},
            auth=False,
        ),
        400,
        "validation_error",
    )
    assert refused["errors"][0]["field"] == "otp_channel"


def test_no_alert_on_registration_or_a_known_device(world: World) -> None:
    app = world.app("fid-channels-quiet")
    email_sign_up(app, EMAIL)
    assert ALERT not in subjects()
    again = app.post("auth/login/", {"email": EMAIL, "password": PASSWORD}, auth=False)
    assert again.status_code == 200 and again.json()["device_key"] is None, again.content
    assert ALERT not in subjects()


def test_an_account_without_an_email_gets_no_alert(world: World) -> None:
    sign_up(world.app("fid-channels-noemail"), PHONE)
    other = world.app("fid-channels-noemail-2")
    login = {"phone": PHONE, "password": PASSWORD}
    other.post("auth/login/", login, auth=False)
    token = verify(other, {"phone": PHONE})
    signed = other.post(
        "auth/login/", {**login, "verification_token": token.json()["verification_token"]}, auth=False
    )
    assert signed.status_code == 200, signed.content
    assert fake_email().sent == []


def test_password_reset_and_change_are_alerted_by_email(world: World) -> None:
    app = both_identifiers(world)
    changed = app.post(
        "me/password/",
        {"current_password": PASSWORD, "password": "Amber-Lantern-River42"},
        idem=True,
    )
    assert changed.status_code in (200, 204), changed.content
    assert "Glow Derma: parolingiz o‘zgartirildi" in subjects()
    other = world.app("fid-channels-reset")
    sent = other.post("auth/send-code/", {"email": EMAIL, "purpose": "reset_password"}, auth=False)
    assert sent.status_code == 200, sent.content
    token = other.post(
        "auth/verify-code/",
        {"email": EMAIL, "purpose": "reset_password", "code": settings.OTP_FAKE_CODE},
        auth=False,
    ).json()["verification_token"]
    reset = other.post(
        "auth/reset-password/", {"verification_token": token, "password": "Copper-Meadow-Dawn77"}, auth=False
    )
    assert reset.status_code == 200, reset.content
    assert "Glow Derma: parolingiz tiklandi" in subjects()
    for message in fake_email().sent:  # alerts carry no code
        if message.subject != "Glow Derma: tasdiqlash kodi":
            assert settings.OTP_FAKE_CODE not in message.text
