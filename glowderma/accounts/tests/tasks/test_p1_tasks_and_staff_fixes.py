# ruff: noqa: S105, S106 - test credentials
"""P1 handlers (``sms.send``, ``apple.revoke``) and the lead-assigned staff OAuth fixes (admin contract
§2.1)."""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services, tasks
from glowderma.accounts.models import AuthSession, Customer, SmsCode, SocialIdentity, StaffUser
from glowderma.accounts.tasks import otp_delivery
from glowderma.accounts.tests.factories import AuthSessionFactory, start_test_session
from glowderma.core import outbox, secretbox
from glowderma.core.models import OutboxMessage
from glowderma.integrations import services as integration_services
from glowderma.integrations.devsms import registry as sms_registry
from glowderma.integrations.devsms.fake import FakeSms
from glowderma.integrations.devsms.live import LiveDevsms
from glowderma.integrations.devsms.ports import OtpPurpose, OtpSms, SmsDelivery, SmsUnavailable
from glowderma.integrations.oauth import fake as oauth_fake
from glowderma.integrations.oauth.registry import fake_apple

pytestmark = pytest.mark.django_db
PHONE = "+998901231231"


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    sms_registry.fake_sms().sent.clear()
    fake_apple().revoked.clear()
    yield
    sms_registry.fake_sms().sent.clear()
    fake_apple().revoked.clear()


def issue(phone: str = PHONE, platform: str = "ios") -> OutboxMessage:
    services.send_code(
        phone, "register", ip="203.0.113.1", fid="fid-tasks-0001", lang="uz", platform=platform
    )
    row = SmsCode.objects.get(phone=phone)
    return OutboxMessage.objects.get(dedupe_key=f"sms.send:{row.pk}")


# --- sms.send


def test_claims_before_sending_and_never_resends(monkeypatch: pytest.MonkeyPatch) -> None:
    message = issue()
    seen: list[Any] = []
    original = FakeSms.send_otp

    def spy(self: FakeSms, sms: OtpSms) -> SmsDelivery:
        seen.append(SmsCode.objects.get(pk=sms.sms_code_id).sent_at)
        return original(self, sms)

    monkeypatch.setattr(FakeSms, "send_otp", spy)
    tasks.send_sms(message)
    tasks.send_sms(message)  # a redelivery
    assert len(seen) == 1 and seen[0] is not None
    assert len(sms_registry.fake_sms().sent) == 1
    row = SmsCode.objects.get(phone=PHONE)
    assert row.provider_message_id == f"fake-{row.pk}"


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("register", OtpPurpose.REGISTRATION),
        ("reset_password", OtpPurpose.PASSWORD_RESET),
        ("link_social", OtpPurpose.ACTION_CONFIRMATION),
        ("attach_phone", OtpPurpose.ACTION_CONFIRMATION),
    ],
)
def test_the_code_goes_out_with_the_template_of_its_purpose(purpose: str, expected: OtpPurpose) -> None:
    """Every stored SMS purpose has a universal OTP template; the plain code is the one the row's HMAC
    holds."""
    if purpose == "reset_password":
        Customer.objects.create(phone=PHONE, password="x")
    services.send_code(
        PHONE,
        purpose,
        ip="203.0.113.1",
        fid="fid-tasks-0001",
        lang="ru",
        platform="ios",
        social_jti="jti-1" if purpose == "link_social" else None,
    )
    row = SmsCode.objects.get(phone=PHONE)
    tasks.send_sms(OutboxMessage.objects.get(dedupe_key=f"sms.send:{row.pk}"))
    [sms] = sms_registry.fake_sms().sent
    assert (sms.phone, sms.purpose, sms.sms_code_id) == (PHONE, expected, row.pk)
    assert services.code_hmac(sms.code) == row.code_hmac


def test_every_sms_purpose_has_a_template() -> None:
    sms_purposes = {stored for stored in SmsCode.Purpose.values if stored != SmsCode.Purpose.VERIFY_EMAIL}
    assert set(otp_delivery.SMS_PURPOSES) == sms_purposes
    assert otp_delivery.SMS_PURPOSES[SmsCode.Purpose.NEW_DEVICE] == OtpPurpose.LOGIN


def test_the_idempotency_key_is_stable_per_code_and_distinct_across_codes() -> None:
    issue()
    row = SmsCode.objects.get(phone=PHONE)
    key = otp_delivery.sms_idempotency_key
    assert key(row) == key(SmsCode.objects.get(pk=row.pk))
    later = row.created_at.replace(microsecond=(row.created_at.microsecond + 1) % 10**6)
    assert key(SmsCode(pk=row.pk, created_at=later)) != key(row)  # the same id in another database


def test_a_repeatable_failure_unclaims_keeps_the_payload_and_repeats_with_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = issue()
    keys: list[str] = []

    def down(self: FakeSms, sms: OtpSms) -> SmsDelivery:
        keys.append(sms.idempotency_key)
        raise SmsUnavailable("read timeout", sent=False)

    monkeypatch.setattr(FakeSms, "send_otp", down)
    for _ in range(2):
        with pytest.raises(SmsUnavailable):
            tasks.send_sms(message)
    row = SmsCode.objects.get(phone=PHONE)
    assert row.sent_at is None
    assert cache.get(services.otp_payload_key(row.pk)) is not None
    assert keys[0] == keys[1] == otp_delivery.sms_idempotency_key(row)


def test_connect_failure_unclaims_and_keeps_the_payload() -> None:
    message = issue("+998901239999")
    row = SmsCode.objects.get(phone="+998901239999")
    with pytest.raises(SmsUnavailable):
        tasks.send_sms(message)
    row.refresh_from_db()
    assert row.sent_at is None
    assert cache.get(services.otp_payload_key(row.pk)) is not None


def test_an_unknown_outcome_keeps_the_claim_and_the_provider_id(monkeypatch: pytest.MonkeyPatch) -> None:
    message = issue()

    def unknown(self: FakeSms, sms: OtpSms) -> SmsDelivery:
        raise SmsUnavailable("provider outcome unknown", sent=True, message_id="81234")

    monkeypatch.setattr(FakeSms, "send_otp", unknown)
    tasks.send_sms(message)
    row = SmsCode.objects.get(phone=PHONE)
    assert row.sent_at is not None and row.provider_message_id == "81234"
    assert cache.get(services.otp_payload_key(row.pk)) is None


def test_rejection_is_final_and_recorded() -> None:
    message = issue("+998901230000")
    tasks.send_sms(message)
    assert integration_services.get_state("devsms").last_error.startswith("rejected")
    assert SmsCode.objects.get(phone="+998901230000").sent_at is not None


def test_missing_ciphertext_sends_nothing() -> None:
    message = issue()
    cache.delete(services.otp_payload_key(SmsCode.objects.get(phone=PHONE).pk))
    tasks.send_sms(message)
    assert sms_registry.fake_sms().sent == []


def test_tampered_ciphertext_sends_nothing(caplog: pytest.LogCaptureFixture) -> None:
    message = issue()
    row = SmsCode.objects.get(phone=PHONE)
    cache.set(services.otp_payload_key(row.pk), secretbox.seal(b"999999", aad=b"another-row"), 120)
    with caplog.at_level(logging.ERROR):
        tasks.send_sms(message)
    assert sms_registry.fake_sms().sent == []


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_fake_sms_in_production_keeps_nothing_and_the_fixed_code_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeSms()
    sms = OtpSms(phone=PHONE, code="123456", purpose=OtpPurpose.LOGIN, sms_code_id=7, idempotency_key="k")
    with caplog.at_level(logging.INFO):
        assert fake.send_otp(sms) == SmsDelivery(message_id="fake-7", status="sent")
    assert fake.sent == []
    assert all("123456" not in record.getMessage() for record in caplog.records)
    assert any("sms.fake_accepted sms_code_id=7" in record.getMessage() for record in caplog.records)
    codes = {sms_registry.new_code() for _ in range(5)}
    assert codes != {"123456"} and all(len(code) == 6 and code.isdigit() for code in codes)


def test_fixed_code_under_local_settings() -> None:
    assert sms_registry.new_code() == "123456"
    with override_settings(DEVSMS_MODE="live"):
        assert isinstance(sms_registry.get_sms_port(), LiveDevsms)
        codes = {sms_registry.new_code() for _ in range(5)}  # the fixed code is a fake-mode switch only
    assert codes != {"123456"} and all(len(code) == 6 and code.isdigit() for code in codes)


# --- apple.revoke


def revoke_message(identity_id: int, token: str, client_id: str = "uz.glowderma.app") -> OutboxMessage:
    return outbox.enqueue(
        "apple.revoke",
        {
            "identity_id": identity_id,
            "client_id": client_id,
            "sealed_token": services.seal_apple_refresh(token, client_id=client_id),
        },
        dedupe_key=f"apple.revoke:{identity_id}",
    )


def test_apple_revoke_uses_the_stored_client_id() -> None:
    message = revoke_message(11, "refresh-11", "uz.glowderma.beta")
    tasks.revoke_apple_token(message)
    assert fake_apple().revoked == [("refresh-11", "uz.glowderma.beta")]
    message.refresh_from_db()
    assert message.payload["sealed_token"] == ""


def test_apple_revoke_retries_transport_errors_and_accepts_a_400() -> None:
    from glowderma.integrations.oauth.ports import OAuthExchangeError

    down = revoke_message(12, "refresh-down")
    with pytest.raises(OAuthExchangeError):
        tasks.revoke_apple_token(down)
    down.refresh_from_db()
    assert down.payload["sealed_token"]  # kept for the retry
    invalid = revoke_message(13, "refresh-invalid")
    tasks.revoke_apple_token(invalid)  # final, no exception
    invalid.refresh_from_db()
    assert invalid.payload["sealed_token"] == ""


def test_apple_revoke_with_a_foreign_client_id_does_not_open() -> None:
    message = revoke_message(14, "refresh-14", "uz.glowderma.app")
    payload = dict(message.payload)
    payload["client_id"] = "uz.other.app"
    OutboxMessage.objects.filter(pk=message.pk).update(payload=payload)
    message.refresh_from_db()
    tasks.revoke_apple_token(message)
    assert fake_apple().revoked == []


def test_apple_revoke_dead_hook_logs_an_error(caplog: pytest.LogCaptureFixture) -> None:
    message = revoke_message(15, "refresh-15")
    with caplog.at_level(logging.ERROR):
        tasks.apple_revoke_dead(message)
    assert any("apple.revoke_dead identity_id=15" in record.getMessage() for record in caplog.records)


def test_enqueue_falls_back_to_the_first_bundle_id_for_old_rows() -> None:
    from glowderma.accounts.tests.factories import CustomerSocialIdentityFactory

    identity = CustomerSocialIdentityFactory.create(provider="apple", provider_refresh_token="x")
    with override_settings(APPLE_BUNDLE_IDS=["uz.first.app", "uz.second.app"]):
        assert services.apple_client_id(identity) == "uz.first.app"
        identity.provider_client_id = "uz.second.app"
        assert services.apple_client_id(identity) == "uz.second.app"


# --- auth_method


def test_customer_sessions_record_the_method_and_keep_it_across_rotation() -> None:
    customer = Customer.objects.create(phone="+998901231232", name="M")
    started = services.start_customer_session(
        customer.pk, "fid-tasks-0002", "ios", "1.0.0", issue_device_key=True, auth_method="apple"
    )
    services.rotate_customer_refresh(started.raw_refresh, fid="fid-tasks-0002")
    session = AuthSession.objects.get(pk=started.session.pk)
    assert session.rotation == 1 and session.auth_method == "apple"
    assert start_test_session(customer, fid="fid-tasks-0003").session.auth_method == "password"


def test_start_staff_session_refuses_an_unknown_method(staff: StaffUser) -> None:
    with pytest.raises(ValueError, match="auth method"):
        services.start_staff_session(staff, device_label="x", auth_method="sms")


STAFF = "/api/v1/admin/auth/oauth/"


def body(code: str, verifier: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "code": code,
        "code_verifier": verifier or secrets.token_urlsafe(48),
        "redirect_uri": "http://localhost:3000/admin/auth/oauth/google/callback",
        **extra,
    }


def client_for(session: AuthSession) -> APIClient:
    from django.conf import settings

    from glowderma.accounts import tokens

    access, _ = tokens.issue_access(session)
    client = APIClient()
    client.default_format = "json"
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def test_link_needs_a_password_session(staff: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = oauth_fake.FakeGoogle.exchange

    def spy(self: oauth_fake.FakeGoogle, **kwargs: Any) -> Any:
        calls.append(kwargs["code"])
        return original(self, **kwargs)

    monkeypatch.setattr(oauth_fake.FakeGoogle, "exchange", spy)
    social_session, _ = services.start_staff_session(staff, device_label="x", auth_method="google")
    refused = client_for(social_session).post(STAFF + "google/link/", body("fake:9:a@b.c", nonce="nonce-9"))
    assert (refused.status_code, refused.data["code"]) == (403, "permission_denied")
    assert refused.data["message"] == "Ruxsat yo‘q."
    legacy = AuthSessionFactory.create(staff=staff, auth_method="")
    assert (
        client_for(legacy).post(STAFF + "google/link/", body("fake:9:a@b.c", nonce="nonce-9")).status_code
        == 403
    )
    assert calls == []  # refused before the provider is called
    password = AuthSessionFactory.create(staff=staff)
    assert password.auth_method == "password"
    ok = client_for(password).post(STAFF + "google/link/", body("fake:9:a@b.c", nonce="nonce-9"))
    assert ok.status_code == 200, ok.data
    assert SocialIdentity.objects.filter(staff=staff, provider="google").exists()


def test_social_login_session_is_a_google_session(staff: StaffUser, anon_client: APIClient) -> None:
    SocialIdentity.objects.create(staff=staff, provider="google", subject="42", email_at_link=staff.email)
    response = anon_client.post(STAFF + "google/", body(f"fake:42:{staff.email}", nonce="nonce-42"))
    assert response.status_code == 200
    assert AuthSession.objects.get(staff=staff).auth_method == "google"


def test_reused_code_verifier_is_refused_before_the_provider(
    staff: StaffUser, anon_client: APIClient, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    SocialIdentity.objects.create(staff=staff, provider="google", subject="42", email_at_link=staff.email)
    verifier = secrets.token_urlsafe(48)
    first = anon_client.post(STAFF + "google/", body(f"fake:42:{staff.email}", verifier, nonce="nonce-42"))
    assert first.status_code == 200
    calls: list[str] = []
    monkeypatch.setattr(oauth_fake.FakeGoogle, "exchange", lambda self, **kw: calls.append("x"))
    again = anon_client.post(STAFF + "google/", body(f"fake:42:{staff.email}", verifier, nonce="nonce-42"))
    assert (again.status_code, again.data["code"]) == (401, "social_account_not_allowed")
    link = staff_client.post(STAFF + "facebook/link/", body("fake:fb:a@b.c", verifier))
    assert (link.status_code, link.data["code"]) == (401, "social_account_not_allowed")
    assert calls == []


def test_redirect_uri_per_provider_with_the_deprecated_fallback(
    staff: StaffUser, anon_client: APIClient
) -> None:
    SocialIdentity.objects.create(staff=staff, provider="google", subject="42", email_at_link=staff.email)
    google = "https://admin.example.uz/admin/auth/oauth/google/callback"
    facebook = "https://admin.example.uz/admin/auth/oauth/facebook/callback"
    login = {"nonce": "nonce-42"}
    with override_settings(GOOGLE_OAUTH_REDIRECT_URI=google, FACEBOOK_OAUTH_REDIRECT_URI=facebook):
        wrong = anon_client.post(
            STAFF + "google/", body(f"fake:42:{staff.email}", redirect_uri=facebook, **login)
        )
        assert wrong.data["errors"][0]["field"] == "redirect_uri"
        ok = anon_client.post(STAFF + "google/", body(f"fake:42:{staff.email}", redirect_uri=google, **login))
        assert ok.status_code == 200
    with override_settings(GOOGLE_OAUTH_REDIRECT_URI="", OAUTH_REDIRECT_URI=google):
        ok = anon_client.post(STAFF + "google/", body(f"fake:42:{staff.email}", redirect_uri=google, **login))
        assert ok.status_code == 200
        wrong = anon_client.post(
            STAFF + "google/", body(f"fake:42:{staff.email}", redirect_uri=facebook, **login)
        )
        assert wrong.status_code == 400
