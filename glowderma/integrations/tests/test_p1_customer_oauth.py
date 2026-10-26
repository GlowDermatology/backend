# ruff: noqa: S105, S106 - test credentials and token-type literals
"""Customer app-token verification (customer contract §2.1.8): fakes, the live Google, Facebook and Apple
adapters (recorded transports, locally signed tokens) and the registry gates."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings
from rest_framework.exceptions import NotFound

from glowderma.integrations.oauth import registry
from glowderma.integrations.oauth.apple import REVOKE_URL, TOKEN_URL, AppleProvider, nonce_digest
from glowderma.integrations.oauth.facebook import FacebookOAuthProvider
from glowderma.integrations.oauth.fake import FakeApple, FakeFacebookApp, FakeGoogleApp
from glowderma.integrations.oauth.google import GoogleAppTokenVerifier
from glowderma.integrations.oauth.ports import OAuthExchangeError

from .conftest import KID, Recorder, StaticJWKClient, connect_error

GOOGLE_IOS = "ios-client.apps.googleusercontent.com"
BUNDLE = "uz.glowderma.app"


@pytest.fixture
def sign(rsa_private_pem: bytes) -> Callable[..., str]:
    def factory(**claims: Any) -> str:
        now = int(time.time())
        base = {"iat": now, "exp": now + 600, "sub": "sub-1"}
        base.update(claims)
        return jwt.encode(
            {k: v for k, v in base.items() if v is not None}, rsa_private_pem, "RS256", headers={"kid": KID}
        )

    return factory


# --- fakes


def test_fake_verifiers() -> None:
    claims = FakeGoogleApp().verify_app_token("fake:g-1:a@gmail.com", "id_token", "n")
    assert (claims.provider, claims.subject, claims.email) == ("google", "g-1", "a@gmail.com")
    assert FakeFacebookApp().verify_app_token("fake-unverified:f-1:", "access_token", None).email == ""
    apple = FakeApple().verify_app_token("fake-apple:a-1:x@privaterelay.appleid.com", "id_token", "n")
    assert apple.audience == BUNDLE
    for token, kind in (
        ("fake-fail", "exchange_failed"),
        ("fake-down", "unavailable"),
        ("fake-expired", "invalid_token"),
    ):
        with pytest.raises(OAuthExchangeError) as error:
            FakeGoogleApp().verify_app_token(token, "id_token", "n")
        assert error.value.kind == kind
    with pytest.raises(OAuthExchangeError):
        FakeApple().verify_app_token("fake:g-1:a@b.c", "id_token", "n")  # not an Apple token
    with override_settings(APPLE_BUNDLE_IDS=["uz.only.app"]), pytest.raises(OAuthExchangeError):
        FakeApple().verify_app_token("fake-apple:a-1:x@y.z:uz.other.app", "id_token", "n")


def test_fake_apple_exchange_and_revoke() -> None:
    fake = FakeApple()
    assert fake.exchange_code("fake-code:abc", client_id=BUNDLE, subject="a-1") == "fake-apple-refresh-a-1"
    for code, kind in (
        ("fake-code-fail", "exchange_failed"),
        ("fake-code-down", "unavailable"),
        ("x", "exchange_failed"),
    ):
        with pytest.raises(OAuthExchangeError) as error:
            fake.exchange_code(code, client_id=BUNDLE, subject="a-1")
        assert error.value.kind == kind
    fake.revoke("r-1", client_id=BUNDLE)
    assert fake.revoked == [("r-1", BUNDLE)]
    with override_settings(DEV_FEATURES_ALLOWED=False):
        fake.revoke("r-2", client_id=BUNDLE)
    assert fake.revoked == [("r-1", BUNDLE)]  # nothing kept under the production settings


# --- registry


def test_customer_providers_and_staff_providers() -> None:
    assert registry.STAFF_PROVIDERS == registry.PROVIDER_NAMES == ("google", "facebook")
    assert registry.CUSTOMER_PROVIDERS == ("google", "facebook", "apple")
    with pytest.raises(NotFound):
        registry.get_oauth_provider("apple")
    with pytest.raises(NotFound):
        registry.get_app_verifier("twitter")
    assert isinstance(registry.get_app_verifier("apple"), FakeApple)


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_fake_oauth_in_production_refuses_every_lookup() -> None:
    for name in ("google", "facebook"):
        with pytest.raises(NotFound):
            registry.get_oauth_provider(name)
    for name in registry.CUSTOMER_PROVIDERS:
        with pytest.raises(NotFound):
            registry.get_app_verifier(name)
    with pytest.raises(NotFound):
        registry.get_apple_service()
    assert isinstance(registry.get_apple_service(require_available=False), FakeApple)


def test_live_customer_adapters() -> None:
    with override_settings(OAUTH_MODE="live", GOOGLE_APP_CLIENT_IDS=[GOOGLE_IOS], APPLE_BUNDLE_IDS=[]):
        assert isinstance(registry.get_app_verifier("google"), GoogleAppTokenVerifier)
        assert isinstance(registry.get_app_verifier("facebook"), FacebookOAuthProvider)
        with pytest.raises(NotFound):
            registry.get_app_verifier("apple")
    with override_settings(OAUTH_MODE="live", APPLE_BUNDLE_IDS=[BUNDLE], APPLE_TEAM_ID="T", APPLE_KEY_ID="K"):
        assert isinstance(registry.get_app_verifier("apple"), AppleProvider)
        assert isinstance(registry.get_apple_service(), AppleProvider)


# --- Google


def test_google_app_token(jwk_client: StaticJWKClient, sign: Callable[..., str]) -> None:
    verifier = GoogleAppTokenVerifier(client_ids=[GOOGLE_IOS, "web.apps"], jwk_client=jwk_client)
    token = sign(iss="https://accounts.google.com", aud=GOOGLE_IOS, nonce="raw-nonce")
    claims = verifier.verify_app_token(token, "id_token", "raw-nonce")
    assert (claims.subject, claims.email, claims.audience) == ("sub-1", "", GOOGLE_IOS)
    with pytest.raises(OAuthExchangeError, match="nonce"):
        verifier.verify_app_token(token, "id_token", "other")
    with pytest.raises(OAuthExchangeError):
        verifier.verify_app_token(
            sign(iss="https://accounts.google.com", aud="someone-else", nonce="n"), "id_token", "n"
        )
    with pytest.raises(OAuthExchangeError):
        verifier.verify_app_token(
            sign(iss="https://accounts.google.com", aud=GOOGLE_IOS), "id_token", "n"
        )  # no nonce
    with pytest.raises(OAuthExchangeError):
        verifier.verify_app_token(token, "access_token", "raw-nonce")
    down = GoogleAppTokenVerifier(client_ids=[GOOGLE_IOS], jwk_client=StaticJWKClient(None))
    with pytest.raises(OAuthExchangeError) as error:
        down.verify_app_token(token, "id_token", "raw-nonce")
    assert error.value.kind == "unavailable"


# --- Facebook


APP_ID = "1234567890123456"


def facebook(recorder: Recorder, jwk_client: StaticJWKClient | None = None) -> FacebookOAuthProvider:
    return FacebookOAuthProvider(
        app_id=APP_ID, app_secret="fb-secret", transport=recorder.transport, jwk_client=jwk_client
    )


def test_facebook_access_token_posts_the_user_token_in_the_body(recorder: Recorder) -> None:
    recorder.route(
        "/debug_token",
        httpx.Response(200, json={"data": {"app_id": APP_ID, "is_valid": True, "expires_at": 1999999999}}),
    )
    recorder.route("/v21.0/me", httpx.Response(200, json={"id": "fb-7", "email": "m@x.uz"}))
    claims = facebook(recorder).verify_app_token("EAAuser", "access_token", None)
    assert (claims.subject, claims.email, claims.expires_at) == ("fb-7", "m@x.uz", 1999999999)
    debug, me = recorder.requests
    assert debug.method == "POST" and "input_token" not in str(debug.url)
    assert parse_qs(debug.content.decode()) == {"input_token": ["EAAuser"]}
    assert "EAAuser" not in str(me.url) and me.headers["Authorization"] == "Bearer EAAuser"


def test_facebook_limited_login_id_token(
    jwk_client: StaticJWKClient, sign: Callable[..., str], recorder: Recorder
) -> None:
    provider = facebook(recorder, jwk_client)
    token = sign(iss="https://www.facebook.com", aud=APP_ID, nonce="n-1", email="a@b.c")
    claims = provider.verify_app_token(token, "id_token", "n-1")
    assert (claims.subject, claims.email) == ("sub-1", "a@b.c")
    with pytest.raises(OAuthExchangeError):
        provider.verify_app_token(token, "id_token", "n-2")
    with pytest.raises(OAuthExchangeError):
        provider.verify_app_token(token, "refresh_token", "n-1")
    assert recorder.requests == []


# --- Apple


@pytest.fixture(scope="module")
def ec_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def apple(recorder: Recorder, ec_pem: bytes, jwk_client: StaticJWKClient | None = None) -> AppleProvider:
    return AppleProvider(
        bundle_ids=[BUNDLE, "uz.glowderma.beta"],
        team_id="TEAM123456",
        key_id="KEY1234567",
        private_key=ec_pem,
        transport=recorder.transport,
        jwk_client=jwk_client,
    )


def test_apple_id_token_with_hashed_nonce(
    jwk_client: StaticJWKClient, sign: Callable[..., str], recorder: Recorder, ec_pem: bytes
) -> None:
    provider = apple(recorder, ec_pem, jwk_client)
    token = sign(
        iss="https://appleid.apple.com",
        aud="uz.glowderma.beta",
        nonce=nonce_digest("raw"),
        email_verified="true",
    )
    claims = provider.verify_app_token(token, "id_token", "raw")
    assert (claims.subject, claims.audience, claims.email) == ("sub-1", "uz.glowderma.beta", "")
    with pytest.raises(OAuthExchangeError):
        provider.verify_app_token(token, "id_token", nonce_digest("raw"))  # the hash is not the raw nonce
    with pytest.raises(OAuthExchangeError):
        provider.verify_app_token(
            sign(iss="https://appleid.apple.com", aud="uz.other", nonce=nonce_digest("raw")),
            "id_token",
            "raw",
        )


def test_apple_code_exchange_and_revoke(
    recorder: Recorder, ec_pem: bytes, jwk_client: StaticJWKClient, sign: Callable[..., str]
) -> None:
    provider = apple(recorder, ec_pem, jwk_client)
    id_token = sign(iss="https://appleid.apple.com", aud="uz.glowderma.beta", sub="s")
    recorder.route(
        "/auth/token", httpx.Response(200, json={"refresh_token": "r-apple", "id_token": id_token})
    )
    recorder.route("/auth/revoke", httpx.Response(200))
    assert provider.exchange_code("code-1", client_id="uz.glowderma.beta", subject="s") == "r-apple"
    provider.revoke("r-apple", client_id="uz.glowderma.beta")
    exchange, revoke = recorder.requests
    assert str(exchange.url) == TOKEN_URL and str(revoke.url) == REVOKE_URL
    form = parse_qs(exchange.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["client_id"] == ["uz.glowderma.beta"]
    secret = form["client_secret"][0]
    header = jwt.get_unverified_header(secret)
    assert header["alg"] == "ES256" and header["kid"] == "KEY1234567"
    public = serialization.load_pem_private_key(ec_pem, password=None).public_key()
    decoded = jwt.decode(secret, public, algorithms=["ES256"], audience="https://appleid.apple.com")  # type: ignore[arg-type]
    assert decoded["iss"] == "TEAM123456" and decoded["sub"] == "uz.glowderma.beta"
    revoked = parse_qs(revoke.content.decode())
    assert revoked["token"] == ["r-apple"] and revoked["token_type_hint"] == ["refresh_token"]


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), "exchange_failed"),
        (httpx.Response(503), "unavailable"),
        (httpx.Response(200, json={}), "exchange_failed"),
        (connect_error, "unavailable"),
    ],
)
def test_apple_failures(recorder: Recorder, ec_pem: bytes, response: Any, kind: str) -> None:
    provider = apple(recorder, ec_pem)
    recorder.route("/auth/token", response)
    recorder.route("/auth/revoke", response)
    with pytest.raises(OAuthExchangeError) as error:
        provider.exchange_code("c", client_id=BUNDLE, subject="s")
    assert error.value.kind == kind
    if kind == "unavailable":
        with pytest.raises(OAuthExchangeError) as revoke_error:
            provider.revoke("r", client_id=BUNDLE)
        assert revoke_error.value.kind == "unavailable"


# --- fix pass 1: the code is bound to the identity's subject; only "token already invalid" is final


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "another-apple-user", "aud": BUNDLE},  # a code of another Apple account
        {"sub": "s", "aud": "uz.glowderma.beta"},  # issued to another client_id
        {"sub": "s", "aud": BUNDLE, "iss": "https://evil.example"},
    ],
)
def test_apple_exchange_refuses_an_id_token_of_another_subject_or_client(
    recorder: Recorder,
    ec_pem: bytes,
    jwk_client: StaticJWKClient,
    sign: Callable[..., str],
    claims: dict[str, str],
) -> None:
    provider = apple(recorder, ec_pem, jwk_client)
    id_token = sign(**{"iss": "https://appleid.apple.com", **claims})
    recorder.route("/auth/token", httpx.Response(200, json={"refresh_token": "r", "id_token": id_token}))
    with pytest.raises(OAuthExchangeError) as error:
        provider.exchange_code("c", client_id=BUNDLE, subject="s")
    assert error.value.kind == "exchange_failed"


def test_apple_exchange_without_id_token_is_refused(recorder: Recorder, ec_pem: bytes) -> None:
    provider = apple(recorder, ec_pem)
    recorder.route("/auth/token", httpx.Response(200, json={"refresh_token": "r"}))
    with pytest.raises(OAuthExchangeError) as error:
        provider.exchange_code("c", client_id=BUNDLE, subject="s")
    assert error.value.kind == "exchange_failed"


def test_apple_exchange_with_jwks_down_is_unavailable(
    recorder: Recorder, ec_pem: bytes, sign: Callable[..., str]
) -> None:
    provider = apple(recorder, ec_pem, StaticJWKClient(None))
    id_token = sign(iss="https://appleid.apple.com", aud=BUNDLE, sub="s")
    recorder.route("/auth/token", httpx.Response(200, json={"refresh_token": "r", "id_token": id_token}))
    with pytest.raises(OAuthExchangeError) as error:
        provider.exchange_code("c", client_id=BUNDLE, subject="s")
    assert error.value.kind == "unavailable"


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), "exchange_failed"),
        (httpx.Response(400, json={"error": "invalid_request"}), "exchange_failed"),
        (httpx.Response(400, json={"error": "invalid_client"}), "unavailable"),
        (httpx.Response(401, json={"error": "invalid_client"}), "unavailable"),
        (httpx.Response(403), "unavailable"),
    ],
)
def test_apple_revoke_treats_only_an_invalid_token_as_final(
    recorder: Recorder, ec_pem: bytes, response: httpx.Response, kind: str
) -> None:
    provider = apple(recorder, ec_pem)
    recorder.route("/auth/revoke", response)
    with pytest.raises(OAuthExchangeError) as error:
        provider.revoke("r", client_id=BUNDLE)
    assert error.value.kind == kind


def test_fake_apple_code_bound_to_another_subject_is_refused() -> None:
    fake = FakeApple()
    assert (
        fake.exchange_code("fake-code-sub:a-1", client_id=BUNDLE, subject="a-1") == "fake-apple-refresh-a-1"
    )
    with pytest.raises(OAuthExchangeError) as error:
        fake.exchange_code("fake-code-sub:someone-else", client_id=BUNDLE, subject="a-1")
    assert error.value.kind == "exchange_failed"
