import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.exceptions import PyJWKClientConnectionError

from glowderma.integrations.oauth import http

CLIENT_ID = "123456789.apps.googleusercontent.com"
KID = "test-kid-1"


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture(scope="session")
def rsa_private_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture(scope="session")
def jwks(rsa_private_pem: bytes) -> dict[str, Any]:
    private_key = serialization.load_pem_private_key(rsa_private_pem, password=None)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


class StaticJWKClient(jwt.PyJWKClient):
    """A ``PyJWKClient`` that serves a fixed key set (or fails) instead of fetching Google's."""

    def __init__(self, key_set: dict[str, Any] | None) -> None:
        super().__init__("https://jwks.invalid/certs", cache_keys=True)
        self.key_set = key_set
        self.fetches = 0

    def fetch_data(self) -> Any:
        self.fetches += 1
        if self.key_set is None:
            raise PyJWKClientConnectionError("jwks down")
        return self.key_set


@pytest.fixture
def jwk_client(jwks: dict[str, Any]) -> StaticJWKClient:
    return StaticJWKClient(jwks)


@pytest.fixture
def make_id_token(rsa_private_pem: bytes) -> Callable[..., str]:
    """Sign a Google-shaped ID token; keyword overrides replace claims, ``kid`` the header key id."""

    def factory(*, kid: str = KID, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": "https://accounts.google.com",
            "azp": CLIENT_ID,
            "aud": CLIENT_ID,
            "sub": "110169484474386276334",
            "email": "ali@glowderma.uz",
            "email_verified": True,
            "nonce": "nonce-abc",
            "hd": "glowderma.uz",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, rsa_private_pem, algorithm="RS256", headers={"kid": kid})

    return factory


class Recorder:
    """An ``httpx.MockTransport`` whose handler is scripted per URL path and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def route(self, path: str, handler: Callable[[httpx.Request], httpx.Response] | httpx.Response) -> None:
        self.routes[path] = handler if callable(handler) else (lambda request, r=handler: r)  # type: ignore[misc]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"error": "no route"})
        return handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    yield Recorder()


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)
