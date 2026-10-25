import hashlib
import hmac
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from glowderma.integrations.oauth.facebook import GRAPH_URL, FacebookOAuthProvider, appsecret_proof
from glowderma.integrations.oauth.ports import OAuthExchangeError

from .conftest import Recorder, connect_error

APP_ID = "1234567890123456"
APP_SECRET = "fb-app-secret-0123456789abcdef"  # noqa: S105
ACCESS_TOKEN = "EAAJZCxQ3ZBz9kBO1example"  # noqa: S105
VERSION = "v21.0"
TOKEN_PATH = f"/{VERSION}/oauth/access_token"  # noqa: S105
DEBUG_PATH = "/debug_token"
ME_PATH = f"/{VERSION}/me"
VERIFIER = "v" * 64
REDIRECT = "https://admin.example.uz/admin/auth/callback"
# Realistic Graph API answers (values illustrative).
TOKEN_RESPONSE = {"access_token": ACCESS_TOKEN, "token_type": "bearer", "expires_in": 5183944}
DEBUG_RESPONSE = {
    "data": {
        "app_id": APP_ID,
        "type": "USER",
        "application": "Glow Derma Admin",
        "data_access_expires_at": 1772000000,
        "expires_at": 1766000000,
        "is_valid": True,
        "issued_at": 1760800000,
        "scopes": ["email", "public_profile"],
        "user_id": "10224567890123456",
    }
}
ME_RESPONSE = {"id": "10224567890123456", "email": "malika@example.com"}
KNOWN_PROOF = (
    "e941110e3d2bfe82621f0e3e1434730d7305d106c5f68c87165d0b27a4611a4a"  # HMAC-SHA256("secret", "token")
)
GRAPH_ERROR = {
    "error": {
        "message": "Invalid verification code format.",
        "type": "OAuthException",
        "code": 100,
        "fbtrace_id": "AbCdEfGh",
    }
}


def read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


def provider(recorder: Recorder) -> FacebookOAuthProvider:
    return FacebookOAuthProvider(
        app_id=APP_ID, app_secret=APP_SECRET, graph_version=VERSION, transport=recorder.transport
    )


def happy(recorder: Recorder, **overrides: dict[str, Any]) -> None:
    recorder.route(TOKEN_PATH, httpx.Response(200, json=overrides.get("token", TOKEN_RESPONSE)))
    recorder.route(DEBUG_PATH, httpx.Response(200, json=overrides.get("debug", DEBUG_RESPONSE)))
    recorder.route(ME_PATH, httpx.Response(200, json=overrides.get("me", ME_RESPONSE)))


def exchange(facebook: FacebookOAuthProvider) -> Any:
    return facebook.exchange(code="AQD3code", code_verifier=VERIFIER, redirect_uri=REDIRECT, nonce=None)


def test_appsecret_proof_is_hmac_sha256_of_token_keyed_by_secret() -> None:
    expected = hmac.new(APP_SECRET.encode(), ACCESS_TOKEN.encode(), hashlib.sha256).hexdigest()
    assert appsecret_proof(APP_SECRET, ACCESS_TOKEN) == expected
    assert appsecret_proof("secret", "token") == KNOWN_PROOF


def test_happy_path_exchanges_debugs_and_reads_profile(recorder: Recorder) -> None:
    happy(recorder)
    claims = exchange(provider(recorder))
    assert claims.provider == "facebook"
    assert claims.subject == "10224567890123456"
    assert claims.email == "malika@example.com"
    assert claims.email_verified is False
    assert claims.hd is None
    token, debug, me = recorder.requests
    assert token.method == "POST" and str(token.url) == f"{GRAPH_URL}{TOKEN_PATH}"
    assert parse_qs(token.content.decode()) == {
        "client_id": [APP_ID],
        "client_secret": [APP_SECRET],
        "code": ["AQD3code"],
        "code_verifier": [VERIFIER],
        "redirect_uri": [REDIRECT],
    }
    assert debug.method == "GET" and debug.url.path == DEBUG_PATH
    assert dict(debug.url.params) == {"input_token": ACCESS_TOKEN}
    assert debug.headers["Authorization"] == f"Bearer {APP_ID}|{APP_SECRET}"
    assert me.method == "GET" and me.url.path == ME_PATH
    assert dict(me.url.params) == {
        "fields": "id,email",
        "appsecret_proof": appsecret_proof(APP_SECRET, ACCESS_TOKEN),
    }
    assert me.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    # Neither the app secret nor the user token may ever appear in a URL (proxy logs, APM, httpx INFO line).
    for request in (token, debug, me):
        assert APP_SECRET not in str(request.url)
    assert ACCESS_TOKEN not in str(me.url)


def test_profile_without_email_is_allowed(recorder: Recorder) -> None:
    happy(recorder, me={"id": "10224567890123456"})
    assert exchange(provider(recorder)).email == ""


def test_profile_without_id_is_invalid_token(recorder: Recorder) -> None:
    happy(recorder, me={"email": "x@example.com"})
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "invalid_token"


def test_debug_token_app_id_mismatch_is_invalid_token(recorder: Recorder) -> None:
    happy(recorder, debug={"data": {**DEBUG_RESPONSE["data"], "app_id": "999"}})
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "invalid_token"
    assert len(recorder.requests) == 2  # /me is never called for a foreign token


def test_debug_token_invalid_is_invalid_token(recorder: Recorder) -> None:
    happy(recorder, debug={"data": {**DEBUG_RESPONSE["data"], "is_valid": False}})
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "invalid_token"
    happy(recorder, debug={"data": "nope"})
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "invalid_token"


def test_debug_token_graph_error_is_invalid_token(recorder: Recorder) -> None:
    happy(recorder)
    recorder.route(DEBUG_PATH, httpx.Response(400, json=GRAPH_ERROR))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "invalid_token"


def test_token_endpoint_400_is_exchange_failed_with_graph_message(recorder: Recorder) -> None:
    recorder.route(TOKEN_PATH, httpx.Response(400, json=GRAPH_ERROR))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "exchange_failed"
    assert info.value.detail == "Invalid verification code format."
    assert len(recorder.requests) == 1


def test_token_response_without_access_token_is_invalid_token(recorder: Recorder) -> None:
    recorder.route(TOKEN_PATH, httpx.Response(200, json={"token_type": "bearer"}))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "invalid_token"


def test_5xx_is_unavailable(recorder: Recorder) -> None:
    recorder.route(TOKEN_PATH, httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "unavailable"
    happy(recorder)
    recorder.route(ME_PATH, httpx.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "unavailable"


def test_transport_error_is_retried_then_unavailable(recorder: Recorder) -> None:
    recorder.route(TOKEN_PATH, connect_error)
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "unavailable"
    assert len(recorder.requests) == 3
    happy(recorder)
    recorder.route(DEBUG_PATH, read_timeout)
    recorder.requests.clear()
    with pytest.raises(OAuthExchangeError) as info:
        exchange(provider(recorder))
    assert info.value.kind == "unavailable"
    assert [request.url.path for request in recorder.requests] == [
        TOKEN_PATH,
        DEBUG_PATH,
        DEBUG_PATH,
        DEBUG_PATH,
    ]
