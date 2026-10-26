"""Live Sign in with Apple adapter (customer contract §2.1.8; product-owner decision 2026-10-26; verify M3).

* **ID token:** RS256 against ``https://appleid.apple.com/auth/keys`` (cached by ``kid``), ``iss =
  https://appleid.apple.com``, ``aud`` ∈ ``APPLE_BUNDLE_IDS``, required ``iss, aud, exp, iat, sub,
  nonce``, and
  ``nonce == sha256(raw_nonce).hexdigest()`` (the app passes the hash to Apple, the raw value to us).
  ``email``
  is optional (often a private relay address); ``email_verified`` is ignored (Apple may send a string).
* **Client secret:** an ES256 JWT (``iss`` = team id, ``sub`` = the bundle id used as ``client_id``, ``aud =
  https://appleid.apple.com``, ``kid`` = key id) signed with the ``.p8`` key of ``APPLE_PRIVATE_KEY_FILE``.
* **Code exchange:** ``POST /auth/token`` (form, ``grant_type=authorization_code``) → Apple's refresh token;
  4xx → ``exchange_failed``, 5xx / transport → ``unavailable``.
* **Revocation:** ``POST /auth/revoke`` (``token_type_hint=refresh_token``); 4xx → ``exchange_failed`` (the
  token is already invalid: final), 5xx / transport → ``unavailable`` (the outbox retries).

Secrets travel only in POST bodies, never in a URL.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import jwt

from . import http
from .google import APP_REQUIRED_CLAIMS, optional_email, verify_rs256
from .ports import ID_TOKEN, AppTokenClaims, OAuthErrorKind, OAuthExchangeError

logger = logging.getLogger(__name__)
APPLE_ISSUER = "https://appleid.apple.com"
JWKS_URL = "https://appleid.apple.com/auth/keys"
TOKEN_URL = "https://appleid.apple.com/auth/token"  # noqa: S105 - an endpoint URL, not a secret
REVOKE_URL = "https://appleid.apple.com/auth/revoke"
CLIENT_SECRET_LIFETIME_SECONDS = 300


def nonce_digest(raw_nonce: str) -> str:
    """The value Apple puts in the token's ``nonce``: lowercase hex SHA-256 of the raw nonce (UTF-8)."""
    return hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()


@cache
def jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(JWKS_URL, cache_keys=True, timeout=http.TIMEOUT_SECONDS)


class AppleProvider:
    name = "apple"

    def __init__(
        self,
        *,
        bundle_ids: list[str],
        team_id: str,
        key_id: str,
        private_key_file: str = "",
        private_key: bytes | None = None,
        transport: httpx.BaseTransport | None = None,
        jwk_client: jwt.PyJWKClient | None = None,
    ) -> None:
        self.bundle_ids = list(bundle_ids)
        self.team_id = team_id
        self.key_id = key_id
        self.private_key_file = private_key_file
        self._private_key = private_key
        self.transport = transport
        self.jwk_client = jwk_client

    # --- ID token ---------------------------------------------------------------------------------------

    def verify_app_token(self, token: str, token_type: str, nonce: str | None) -> AppTokenClaims:
        if token_type != ID_TOKEN or not self.bundle_ids:
            raise OAuthExchangeError("invalid_token", "unsupported token")
        claims = verify_rs256(
            token,
            client=self.jwk_client or jwks_client(),
            audience=self.bundle_ids,
            issuer=APPLE_ISSUER,
            required=APP_REQUIRED_CLAIMS,
        )
        token_nonce = claims.get("nonce")
        if (
            not isinstance(token_nonce, str)
            or nonce is None
            or not hmac.compare_digest(token_nonce.encode("utf-8"), nonce_digest(nonce).encode("utf-8"))
        ):
            raise OAuthExchangeError("invalid_token", "nonce mismatch")
        audience = claims["aud"] if isinstance(claims["aud"], str) else str(claims["aud"][0])
        return AppTokenClaims(
            self.name, str(claims["sub"]), optional_email(claims), audience, int(claims["exp"])
        )

    # --- client secret, code exchange, revocation ------------------------------------------------------

    def _key(self) -> bytes:
        if self._private_key is None:
            self._private_key = Path(self.private_key_file).read_bytes()
        return self._private_key

    def client_secret(self, client_id: str) -> str:
        issued = int(time.time())
        claims = {
            "iss": self.team_id,
            "iat": issued,
            "exp": issued + CLIENT_SECRET_LIFETIME_SECONDS,
            "aud": APPLE_ISSUER,
            "sub": client_id,
        }
        return jwt.encode(claims, self._key(), algorithm="ES256", headers={"kid": self.key_id})

    def _post(self, url: str, data: dict[str, str], *, client_error: OAuthErrorKind) -> dict[str, Any]:
        try:
            with http.client(self.transport) as session:
                response = http.request(session, "POST", url, data=data)
        except httpx.TransportError as exc:
            logger.warning("Apple %s unreachable: %s", url, type(exc).__name__)
            raise OAuthExchangeError("unavailable", "apple unreachable") from exc
        if response.status_code >= 500:
            raise OAuthExchangeError("unavailable", f"apple {response.status_code}")
        body = http.json_body(response)
        if response.status_code >= 400:
            raise OAuthExchangeError(client_error, str(body.get("error", response.status_code)))
        return body

    def exchange_code(self, code: str, *, client_id: str, subject: str) -> str:
        body = self._post(
            TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": self.client_secret(client_id),
                "code": code,
                "grant_type": "authorization_code",
            },
            client_error="exchange_failed",
        )
        refresh_token = body.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise OAuthExchangeError("exchange_failed", "token response without refresh_token")
        return refresh_token

    def revoke(self, refresh_token: str, *, client_id: str) -> None:
        self._post(
            REVOKE_URL,
            {
                "client_id": client_id,
                "client_secret": self.client_secret(client_id),
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
            client_error="exchange_failed",
        )
