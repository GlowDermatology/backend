import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from glowderma.integrations.oauth import http
from glowderma.integrations.oauth.google import TOKEN_URL, GoogleOAuthProvider, jwks_client
from glowderma.integrations.oauth.ports import OAuthExchangeError

from .conftest import CLIENT_ID, Recorder, StaticJWKClient, connect_error

TOKEN_PATH = "/token"  # noqa: S105
# Shape of a real token endpoint answer (values illustrative).
TOKEN_RESPONSE = {
    "access_token": "ya29.a0AfH6SMB...",
    "expires_in": 3599,
    "scope": "openid https://www.googleapis.com/auth/userinfo.email",
    "token_type": "Bearer",
    "id_token": "",
}
VERIFIER = "d" * 43
REDIRECT = "https://admin.example.uz/admin/auth/callback"


def provider(recorder: Recorder, jwk_client: StaticJWKClient, allowed_hd: str = "") -> GoogleOAuthProvider:
    return GoogleOAuthProvider(
        client_id=CLIENT_ID,
        client_secret="google-secret",  # noqa: S106
        allowed_hd=allowed_hd,
        transport=recorder.transport,
        jwk_client=jwk_client,
    )


def token_ok(id_token: str) -> httpx.Response:
    return httpx.Response(200, json={**TOKEN_RESPONSE, "id_token": id_token})


def exchange(google: GoogleOAuthProvider, nonce: str | None = "nonce-abc") -> Any:
    return google.exchange(code="4/0AX4XfWh...", code_verifier=VERIFIER, redirect_uri=REDIRECT, nonce=nonce)


def test_valid_id_token_yields_claims_and_posts_pkce_exchange(
    recorder: Recorder, jwk_client: StaticJWKClient, make_id_token: Callable[..., str]
) -> None:
    recorder.route(TOKEN_PATH, token_ok(make_id_token()))
    claims = exchange(provider(recorder, jwk_client))
    assert claims.provider == "google"
    assert claims.subject == "110169484474386276334"
    assert claims.email == "ali@glowderma.uz"
    assert claims.email_verified is True
    assert claims.hd == "glowderma.uz"
    (request,) = recorder.requests
    assert str(request.url) == TOKEN_URL
    assert request.method == "POST"
    assert parse_qs(request.content.decode()) == {
        "client_id": [CLIENT_ID],
        "client_secret": ["google-secret"],
        "code": ["4/0AX4XfWh..."],
        "code_verifier": [VERIFIER],
        "grant_type": ["authorization_code"],
        "redirect_uri": [REDIRECT],
    }


def test_consumer_account_has_no_hd(
    recorder: Recorder, jwk_client: StaticJWKClient, make_id_token: Callable[..., str]
) -> None:
    recorder.route(TOKEN_PATH, token_ok(make_id_token(hd=None, email="ali@gmail.com")))
    claims = exchange(provider(recorder, jwk_client))
    assert claims.hd is None
    assert claims.email == "ali@gmail.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other-client.apps.googleusercontent.com"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 3600},
        {"nonce": "nonce-other"},
        {"nonce": None},
        {"email_verified": False},
        {"email_verified": None},
        {"email": None},
        {"kid": "unknown-kid"},
    ],
    ids=[
        "wrong-aud",
        "wrong-iss",
        "expired",
        "nonce-mismatch",
        "no-nonce",
        "unverified",
        "no-verified",
        "no-email",
        "unknown-kid",
    ],
)
def test_bad_id_token_is_invalid_token(
    recorder: Recorder,
    jwk_client: StaticJWKClient,
    make_id_token: Callable[..., str],
    overrides: dict[str, Any],
) -> None:
    recorder.route(TOKEN_PATH, token_ok(make_id_token(**overrides)))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "invalid_token"


def test_both_google_issuers_are_accepted(
    recorder: Recorder, jwk_client: StaticJWKClient, make_id_token: Callable[..., str]
) -> None:
    recorder.route(TOKEN_PATH, token_ok(make_id_token(iss="accounts.google.com")))
    assert exchange(provider(recorder, jwk_client)).subject == "110169484474386276334"


def test_allowed_hd_mismatch_is_invalid_token(
    recorder: Recorder, jwk_client: StaticJWKClient, make_id_token: Callable[..., str]
) -> None:
    recorder.route(TOKEN_PATH, token_ok(make_id_token(hd="other.uz", email="ali@other.uz")))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client, allowed_hd="glowderma.uz"))
    assert info.value.kind == "invalid_token"
    recorder.route(TOKEN_PATH, token_ok(make_id_token(hd=None, email="ali@gmail.com")))
    with pytest.raises(OAuthExchangeError):
        exchange(provider(recorder, jwk_client, allowed_hd="glowderma.uz"))
    recorder.route(TOKEN_PATH, token_ok(make_id_token()))
    assert exchange(provider(recorder, jwk_client, allowed_hd="glowderma.uz")).hd == "glowderma.uz"


def test_token_endpoint_400_is_exchange_failed(recorder: Recorder, jwk_client: StaticJWKClient) -> None:
    recorder.route(
        TOKEN_PATH,
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"}),
    )
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "exchange_failed"
    assert info.value.detail == "invalid_grant"
    assert len(recorder.requests) == 1  # a status is never retried


def test_token_endpoint_503_is_unavailable(recorder: Recorder, jwk_client: StaticJWKClient) -> None:
    recorder.route(TOKEN_PATH, httpx.Response(503, text="Service Unavailable"))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "unavailable"
    assert len(recorder.requests) == 1


def test_transport_errors_retry_twice_then_unavailable(
    recorder: Recorder, jwk_client: StaticJWKClient
) -> None:
    recorder.route(TOKEN_PATH, connect_error)
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "unavailable"
    assert len(recorder.requests) == http.RETRY_ATTEMPTS == 3


def test_transport_error_then_success_is_transparent(
    recorder: Recorder, jwk_client: StaticJWKClient, make_id_token: Callable[..., str]
) -> None:
    answers: list[Callable[[httpx.Request], httpx.Response]] = [
        connect_error,
        lambda request: token_ok(make_id_token()),
    ]
    recorder.route(TOKEN_PATH, lambda request: answers.pop(0)(request))
    assert exchange(provider(recorder, jwk_client)).email == "ali@glowderma.uz"
    assert len(recorder.requests) == 2


def test_token_response_without_id_token_is_invalid_token(
    recorder: Recorder, jwk_client: StaticJWKClient
) -> None:
    recorder.route(TOKEN_PATH, httpx.Response(200, json={**TOKEN_RESPONSE, "id_token": None}))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "invalid_token"
    recorder.route(TOKEN_PATH, httpx.Response(200, text="not json"))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "invalid_token"


def test_unreachable_jwks_is_unavailable(recorder: Recorder, make_id_token: Callable[..., str]) -> None:
    recorder.route(TOKEN_PATH, token_ok(make_id_token()))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, StaticJWKClient(None)))
    assert info.value.kind == "unavailable"


def test_garbage_id_token_is_invalid_token(recorder: Recorder, jwk_client: StaticJWKClient) -> None:
    recorder.route(TOKEN_PATH, token_ok("not.a.jwt"))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder, jwk_client))
    assert info.value.kind == "invalid_token"


def test_jwks_key_set_is_cached_across_exchanges(
    recorder: Recorder, jwk_client: StaticJWKClient, make_id_token: Callable[..., str]
) -> None:
    recorder.route(TOKEN_PATH, lambda request: token_ok(make_id_token()))
    google = provider(recorder, jwk_client)
    exchange(google)
    exchange(google)
    assert jwk_client.fetches == 1


def test_default_jwks_client_is_shared_and_cached() -> None:
    client = jwks_client()
    assert client is jwks_client()
    assert client.uri == "https://www.googleapis.com/oauth2/v3/certs"
