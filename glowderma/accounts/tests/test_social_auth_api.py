"""API tests for ``auth/oauth/{provider}/`` (login, link, unlink) with the fake providers (contract 2.1)."""

import secrets
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.models import AuditEntry, IdempotencyRecord
from glowderma.integrations.oauth.ports import OAuthExchangeError

pytestmark = pytest.mark.django_db
BASE = "/api/v1/admin/auth/oauth/"
VERIFIER = "v" * 43  # a fixed verifier where the body is only validated; body() makes a fresh one per call
REDIRECT = "http://localhost:3000/admin/auth/callback"
LOGIN_KEYS = {"access", "access_expires_at", "refresh", "refresh_expires_at", "user"}


def body(code: str, *, nonce: str | None = "*", **overrides: Any) -> dict[str, Any]:
    # A PKCE verifier is single use across instances (admin §2.1): every call gets its own.
    data: dict[str, Any] = {
        "code": code,
        "code_verifier": secrets.token_urlsafe(48),
        "redirect_uri": REDIRECT,
    }
    if nonce is not None:
        data["nonce"] = nonce
    data.update(overrides)
    return data


def link(staff: StaffUser, provider: str, subject: str) -> SocialIdentity:
    return SocialIdentity.objects.create(
        staff=staff, provider=provider, subject=subject, email_at_link=staff.email
    )


def strip(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key != "request_id"}


def test_login_with_linked_google_identity(anon_client: APIClient, staff: StaffUser) -> None:
    link(staff, "google", "42")
    response = anon_client.post(BASE + "google/", body(f"fake:42:{staff.email}", nonce="nonce-42"))
    assert response.status_code == 200, response.data
    assert set(response.data) == LOGIN_KEYS
    assert response.data["user"]["id"] == staff.pk
    assert response.data["user"]["linked_providers"] == ["google"]
    assert response.data["access_expires_at"].endswith("+05:00")
    assert AuthSession.objects.get().device_label == ""
    assert AuditEntry.objects.get(action="auth.login_succeeded").after == {
        "email": staff.email,
        "method": "google",
    }
    # the access token works on an authenticated route
    anon_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    assert anon_client.get("/api/v1/admin/auth/me/").data == response.data["user"]


def test_login_with_linked_facebook_identity_needs_no_nonce(anon_client: APIClient, staff: StaffUser) -> None:
    link(staff, "facebook", "fb-7")
    response = anon_client.post(BASE + "facebook/", body("fake:fb-7:whatever@example.com", nonce=None))
    assert response.status_code == 200, response.data
    assert response.data["user"]["linked_providers"] == ["facebook"]


def test_unknown_identity_gives_identical_401_for_existing_and_unknown_email(
    anon_client: APIClient, staff: StaffUser
) -> None:
    existing = anon_client.post(BASE + "google/", body(f"fake:1:{staff.email}"))
    unknown = anon_client.post(BASE + "google/", body("fake:2:nobody@gmail.com"))
    assert existing.status_code == unknown.status_code == 401
    assert strip(existing.data) == strip(unknown.data)
    assert existing.data["code"] == "social_account_not_allowed"
    assert existing.data["message"] == "Bu hisob administrator sifatida ro‘yxatdan o‘tmagan."
    assert existing.data["errors"] == []
    assert [entry.after for entry in AuditEntry.objects.filter(action="auth.login_failed")] == [
        {"email": staff.email, "method": "google"},
        {"email": "nobody@gmail.com", "method": "google"},
    ]
    assert SocialIdentity.objects.count() == 0


def test_inactive_staff_identity_is_401(anon_client: APIClient) -> None:
    staff = StaffUserFactory.create(is_active=False)
    link(staff, "google", "42")
    response = anon_client.post(BASE + "google/", body(f"fake:42:{staff.email}"))
    assert response.status_code == 401
    assert response.data["code"] == "social_account_not_allowed"


def test_autolink_off_by_default_then_on_for_gmail_and_hd(anon_client: APIClient) -> None:
    gmail = StaffUserFactory.create(email="ali@gmail.com")
    assert anon_client.post(BASE + "google/", body("fake:1:ali@gmail.com")).status_code == 401
    with override_settings(STAFF_GOOGLE_AUTOLINK=True):
        response = anon_client.post(BASE + "google/", body("fake:1:ali@gmail.com"))
        assert response.status_code == 200, response.data
        assert response.data["user"]["id"] == gmail.pk
        assert response.data["user"]["linked_providers"] == ["google"]
        workspace = StaffUserFactory.create(email="vali@glowderma.uz")
        response = anon_client.post(BASE + "google/", body("fake:2:vali@glowderma.uz:glowderma.uz"))
        assert response.status_code == 200, response.data
        assert response.data["user"]["id"] == workspace.pk
        # refused: other domain without matching hd, unverified email, facebook
        StaffUserFactory.create(email="x@other.uz")
        assert anon_client.post(BASE + "google/", body("fake:3:x@other.uz")).status_code == 401
        assert anon_client.post(BASE + "google/", body("fake-unverified:4:ali@gmail.com")).status_code == 401
        assert anon_client.post(BASE + "facebook/", body("fake:5:ali@gmail.com")).status_code == 401
    assert SocialIdentity.objects.count() == 2
    assert AuditEntry.objects.filter(action="auth.social_linked", after__auto=True).count() == 2


def test_unknown_provider_is_404(anon_client: APIClient, staff_client: APIClient) -> None:
    assert anon_client.post(BASE + "github/", body("fake:1:a@gmail.com")).status_code == 404
    assert anon_client.post(BASE + "github/", {}).data["code"] == "not_found"
    assert staff_client.post(BASE + "github/link/", body("fake:1:a@gmail.com")).status_code == 404
    assert staff_client.delete(BASE + "github/").status_code == 404


@pytest.mark.parametrize(
    "payload,field,code",
    [
        (body("fake:1:a@gmail.com", nonce=None), "nonce", "required"),
        (body("fake:1:a@gmail.com", code_verifier="v" * 42), "code_verifier", "min_length"),
        (body("fake:1:a@gmail.com", code_verifier="v" * 129), "code_verifier", "max_length"),
        (body(""), "code", "blank"),
        (body("x" * 2049), "code", "max_length"),
        (body("fake:1:a@gmail.com", redirect_uri="not a url"), "redirect_uri", "invalid"),
        ({"code_verifier": VERIFIER, "redirect_uri": REDIRECT, "nonce": "*"}, "code", "required"),
    ],
    ids=[
        "nonce",
        "verifier-short",
        "verifier-long",
        "code-blank",
        "code-long",
        "uri-invalid",
        "code-missing",
    ],
)
def test_body_validation(anon_client: APIClient, payload: dict[str, Any], field: str, code: str) -> None:
    response = anon_client.post(BASE + "google/", payload)
    assert response.status_code == 400, response.data
    assert response.data["code"] == "validation_error"
    assert {(error["field"], error["code"]) for error in response.data["errors"]} == {(field, code)}


@override_settings(OAUTH_REDIRECT_URI="https://admin.example.uz/admin/auth/callback")
def test_redirect_uri_must_equal_configured_value(anon_client: APIClient, staff: StaffUser) -> None:
    link(staff, "google", "42")
    response = anon_client.post(BASE + "google/", body(f"fake:42:{staff.email}"))
    assert response.status_code == 400
    assert response.data["errors"] == [
        {"field": "redirect_uri", "code": "invalid", "message": "Noto‘g‘ri qiymat."}
    ]
    response = anon_client.post(
        BASE + "google/",
        body(f"fake:42:{staff.email}", redirect_uri="https://admin.example.uz/admin/auth/callback"),
    )
    assert response.status_code == 200, response.data


def test_exchange_failed_and_invalid_token_are_400(anon_client: APIClient, staff: StaffUser) -> None:
    link(staff, "google", "42")
    response = anon_client.post(BASE + "google/", body("fake-fail"))
    assert response.status_code == 400
    assert strip(response.data) == {
        "code": "oauth_exchange_failed",
        "message": "Ijtimoiy tarmoq orqali kirish amalga oshmadi.",
        "errors": [],
    }
    nonce_mismatch = anon_client.post(BASE + "google/", body(f"fake:42:{staff.email}", nonce="nonce-other"))
    assert nonce_mismatch.status_code == 400
    assert nonce_mismatch.data["code"] == "oauth_exchange_failed"
    assert AuthSession.objects.count() == 0
    assert not AuditEntry.objects.filter(action__startswith="auth.login").exists()


def test_provider_down_is_503(anon_client: APIClient) -> None:
    response = anon_client.post(BASE + "facebook/", body("fake-down", nonce=None))
    assert response.status_code == 503
    assert response.data["code"] == "provider_unavailable"
    assert response.data["message"] == "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring."


def test_unexpected_error_kind_maps_to_exchange_failed(
    anon_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from glowderma.integrations.oauth import fake

    def boom(self: Any, **kwargs: Any) -> Any:
        raise OAuthExchangeError("invalid_token", "signature")

    monkeypatch.setattr(fake.FakeGoogle, "exchange", boom)
    response = anon_client.post(BASE + "google/", body("fake:1:a@gmail.com"))
    assert response.status_code == 400
    assert response.data["code"] == "oauth_exchange_failed"


@override_settings(THROTTLE_RATES={"oauth": "2/min"})
def test_login_is_throttled_per_client_ip(anon_client: APIClient) -> None:
    for _ in range(2):
        assert (
            anon_client.post(
                BASE + "google/", body("fake:1:a@gmail.com"), REMOTE_ADDR="203.0.113.5"
            ).status_code
            == 401
        )
    throttled = anon_client.post(BASE + "google/", body("fake:1:a@gmail.com"), REMOTE_ADDR="203.0.113.5")
    assert throttled.status_code == 429
    assert throttled.data["code"] == "throttled"
    assert int(throttled["Retry-After"]) >= 1
    # another IP has its own budget; the DELETE on the same route is not counted against "oauth"
    assert (
        anon_client.post(BASE + "google/", body("fake:1:a@gmail.com"), REMOTE_ADDR="203.0.113.6").status_code
        == 401
    )


def test_oauth_default_rate_is_30_per_minute(anon_client: APIClient) -> None:
    for _ in range(30):
        assert anon_client.post(BASE + "google/", body("fake:1:a@gmail.com")).status_code == 401
    assert anon_client.post(BASE + "google/", body("fake:1:a@gmail.com")).status_code == 429


def test_login_ignores_idempotency_key(anon_client: APIClient, staff: StaffUser) -> None:
    link(staff, "google", "42")
    anon_client.credentials(HTTP_IDEMPOTENCY_KEY="4b4b1f1e-5d3a-4a2b-9c1d-0e0f1a2b3c4d")
    assert anon_client.post(BASE + "google/", body(f"fake:42:{staff.email}")).status_code == 200
    assert anon_client.post(BASE + "google/", body(f"fake:42:{staff.email}")).status_code == 200
    assert AuthSession.objects.count() == 2
    assert IdempotencyRecord.objects.count() == 0


def test_link_then_me_then_unlink(staff_client: APIClient, staff: StaffUser) -> None:
    response = staff_client.post(BASE + "google/link/", body(f"fake:42:{staff.email}", nonce="nonce-42"))
    assert response.status_code == 200, response.data
    assert response.data["id"] == staff.pk
    assert response.data["linked_providers"] == ["google"]
    assert set(response.data) == {
        "id",
        "email",
        "display_name",
        "is_superuser",
        "linked_providers",
        "session_expires_at",
    }
    identity = SocialIdentity.objects.get()
    assert (identity.staff, identity.provider, identity.subject, identity.email_at_link) == (
        staff,
        "google",
        "42",
        staff.email,
    )
    assert AuditEntry.objects.get(action="auth.social_linked").after == {
        "provider": "google",
        "subject": "42",
        "email": staff.email,
        "auto": False,
    }
    response = staff_client.post(BASE + "facebook/link/", body("fake:fb-1:x@example.com", nonce=None))
    assert response.status_code == 200
    assert response.data["linked_providers"] == ["facebook", "google"]
    assert staff_client.get("/api/v1/admin/auth/me/").data["linked_providers"] == ["facebook", "google"]
    assert staff_client.delete(BASE + "google/").status_code == 204
    assert staff_client.get("/api/v1/admin/auth/me/").data["linked_providers"] == ["facebook"]
    assert staff_client.delete(BASE + "facebook/").status_code == 204
    assert staff_client.get("/api/v1/admin/auth/me/").data["linked_providers"] == []
    assert AuditEntry.objects.filter(action="auth.social_unlinked").count() == 2
    assert AuditEntry.objects.get(action="auth.social_unlinked", after__provider="google").after == {
        "provider": "google",
        "subject": "42",
    }


def test_link_refuses_duplicates_with_409(staff_client: APIClient, staff: StaffUser) -> None:
    other = StaffUserFactory.create()
    link(other, "google", "taken")
    taken = staff_client.post(BASE + "google/link/", body("fake:taken:a@gmail.com"))
    assert taken.status_code == 409
    assert strip(taken.data) == {
        "code": "conflict",
        "message": "Bu ijtimoiy hisob allaqachon bog‘langan.",
        "errors": [],
    }
    assert staff_client.post(BASE + "google/link/", body("fake:mine:a@gmail.com")).status_code == 200
    again = staff_client.post(BASE + "google/link/", body("fake:mine-2:a@gmail.com"))
    assert again.status_code == 409
    assert again.data["message"] == "Bu provayder allaqachon bog‘langan."
    assert SocialIdentity.objects.filter(staff=staff).count() == 1


def test_link_validates_body_and_maps_provider_errors(staff_client: APIClient) -> None:
    assert staff_client.post(BASE + "google/link/", body("fake:1:a@gmail.com", nonce=None)).status_code == 400
    assert staff_client.post(BASE + "google/link/", body("fake-fail")).status_code == 400
    assert staff_client.post(BASE + "google/link/", body("fake-fail")).data["code"] == "oauth_exchange_failed"
    assert staff_client.post(BASE + "google/link/", body("fake-down")).status_code == 503
    assert SocialIdentity.objects.count() == 0


def test_unlink_without_identity_is_404(staff_client: APIClient) -> None:
    response = staff_client.delete(BASE + "google/")
    assert response.status_code == 404
    assert response.data["code"] == "not_found"


def test_link_and_unlink_require_bearer_while_login_does_not(
    anon_client: APIClient, staff: StaffUser
) -> None:
    """The per-method split on one route (the auth matrix checks every credential kind; this is a summary)."""
    assert anon_client.post(BASE + "google/link/", body("fake:1:a@gmail.com")).status_code == 401
    assert anon_client.delete(BASE + "google/").status_code == 401
    assert anon_client.get(BASE + "google/").status_code == 401
    assert anon_client.options(BASE + "google/").status_code == 401
    link(staff, "google", "1")
    assert anon_client.post(BASE + "google/", body(f"fake:1:{staff.email}")).status_code == 200
