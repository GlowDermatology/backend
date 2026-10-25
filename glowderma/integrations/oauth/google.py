"""Live Google adapter: authorization-code exchange with PKCE, then ID-token verification against JWKS.

Failure mapping (contract 2.1): token endpoint 4xx -> ``exchange_failed``; 5xx, transport errors and an
unreachable JWKS endpoint -> ``unavailable``; an ID token that fails any check (signature, ``aud``,
``iss``, ``exp``, ``nonce``, ``email_verified``, ``hd``) -> ``invalid_token``.
"""

import logging
import secrets
from functools import cache
from typing import Any

import httpx
import jwt
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from . import http
from .ports import OAuthExchangeError, SocialClaims

logger = logging.getLogger(__name__)
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint URL, not a secret
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
ALGORITHMS = ("RS256",)
REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "sub", "email")
LEEWAY_SECONDS = 30


@cache
def jwks_client() -> jwt.PyJWKClient:
    """One process-wide JWKS client: the key set is cached and refreshed on an unknown ``kid``."""
    return jwt.PyJWKClient(JWKS_URL, cache_keys=True, timeout=http.TIMEOUT_SECONDS)


class GoogleOAuthProvider:
    name = "google"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        allowed_hd: str = "",
        transport: httpx.BaseTransport | None = None,
        jwk_client: jwt.PyJWKClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.allowed_hd = allowed_hd
        self.transport = transport
        self.jwk_client = jwk_client

    def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> SocialClaims:
        token = self._exchange_code(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri)
        id_token = token.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OAuthExchangeError("invalid_token", "token response without id_token")
        return self._verify_id_token(id_token, nonce=nonce)

    def _exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        try:
            with http.client(self.transport) as session:
                response = http.request(session, "POST", TOKEN_URL, data=data)
        except httpx.TransportError as exc:
            logger.warning("Google token endpoint unreachable: %s", type(exc).__name__)
            raise OAuthExchangeError("unavailable", "token endpoint unreachable") from exc
        if response.status_code >= 500:
            raise OAuthExchangeError("unavailable", f"token endpoint {response.status_code}")
        body = http.json_body(response)
        if response.status_code >= 400:
            raise OAuthExchangeError("exchange_failed", str(body.get("error", response.status_code)))
        return body

    def _verify_id_token(self, id_token: str, *, nonce: str | None) -> SocialClaims:
        client = self.jwk_client or jwks_client()
        try:
            signing_key = client.get_signing_key_from_jwt(id_token)
        except PyJWKClientConnectionError as exc:
            raise OAuthExchangeError("unavailable", "jwks unreachable") from exc
        except (PyJWKClientError, jwt.PyJWTError) as exc:
            raise OAuthExchangeError("invalid_token", "unknown signing key") from exc
        try:
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=list(ALGORITHMS),
                audience=self.client_id,
                issuer=ISSUERS,
                leeway=LEEWAY_SECONDS,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as exc:
            raise OAuthExchangeError("invalid_token", type(exc).__name__) from exc
        token_nonce = claims.get("nonce")
        if (
            not isinstance(token_nonce, str)
            or nonce is None
            or not secrets.compare_digest(token_nonce, nonce)
        ):
            raise OAuthExchangeError("invalid_token", "nonce mismatch")
        if claims.get("email_verified") is not True:
            raise OAuthExchangeError("invalid_token", "email not verified")
        hd = claims.get("hd")
        hd = hd if isinstance(hd, str) and hd else None
        if self.allowed_hd and hd != self.allowed_hd:
            raise OAuthExchangeError("invalid_token", "hd not allowed")
        return SocialClaims(
            provider=self.name,
            subject=str(claims["sub"]),
            email=str(claims["email"]),
            email_verified=True,
            hd=hd,
        )
