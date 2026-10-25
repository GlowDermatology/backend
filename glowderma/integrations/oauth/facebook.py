"""Live Facebook adapter: code exchange, ``debug_token`` check, then ``/me`` with ``appsecret_proof``.

Failure mapping (contract 2.1): ``oauth/access_token`` 4xx -> ``exchange_failed``; any 5xx or transport
error -> ``unavailable``; a token that ``debug_token`` does not confirm for our app, or a failing
``/me`` call -> ``invalid_token``. Facebook never vouches for the email, so ``email_verified`` is always
``False`` (the address is informational, stored as ``email_at_link``) and ``hd`` is ``None``.

Secrets never appear in a URL: the code exchange POSTs the app secret in the body, and the app access token
(``app_id|app_secret``) and the user access token are sent as ``Authorization: Bearer`` headers.
"""

import hashlib
import hmac
import logging
from typing import Any

import httpx

from . import http
from .ports import OAuthErrorKind, OAuthExchangeError, SocialClaims

logger = logging.getLogger(__name__)
GRAPH_URL = "https://graph.facebook.com"


def appsecret_proof(app_secret: str, access_token: str) -> str:
    """HMAC-SHA256 of the access token keyed with the app secret (Graph API "appsecret_proof")."""
    return hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


def bearer(token: str) -> dict[str, str]:
    """Graph API accepts any access token (user or ``app_id|app_secret``) as ``Authorization: Bearer``."""
    return {"Authorization": f"Bearer {token}"}


class FacebookOAuthProvider:
    name = "facebook"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        graph_version: str = "v21.0",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.graph_version = graph_version
        self.transport = transport

    def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> SocialClaims:
        with http.client(self.transport) as session:
            access_token = self._exchange_code(
                session, code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
            )
            self._debug_token(session, access_token)
            profile = self._me(session, access_token)
        subject = profile.get("id")
        if not isinstance(subject, str) or not subject:
            raise OAuthExchangeError("invalid_token", "profile without id")
        email = profile.get("email")
        return SocialClaims(
            provider=self.name,
            subject=subject,
            email=email if isinstance(email, str) else "",
            email_verified=False,
            hd=None,
        )

    def _call(
        self, session: httpx.Client, method: str, path: str, *, client_error: OAuthErrorKind, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = http.request(session, method, f"{GRAPH_URL}{path}", **kwargs)
        except httpx.TransportError as exc:
            logger.warning("Facebook Graph %s unreachable: %s", path, type(exc).__name__)
            raise OAuthExchangeError("unavailable", f"{path} unreachable") from exc
        if response.status_code >= 500:
            raise OAuthExchangeError("unavailable", f"{path} {response.status_code}")
        body = http.json_body(response)
        if response.status_code >= 400:
            error = body.get("error")
            detail = error.get("message", "") if isinstance(error, dict) else str(response.status_code)
            raise OAuthExchangeError(client_error, detail)
        return body

    def _exchange_code(
        self, session: httpx.Client, *, code: str, code_verifier: str, redirect_uri: str
    ) -> str:
        body = self._call(
            session,
            "POST",
            f"/{self.graph_version}/oauth/access_token",
            client_error="exchange_failed",
            data={
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
        )
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthExchangeError("invalid_token", "token response without access_token")
        return access_token

    def _debug_token(self, session: httpx.Client, access_token: str) -> None:
        # The app access token embeds the app secret: it travels in the Authorization header, never in the
        # URL, so proxy access logs, APM and httpx's own INFO request line cannot leak it.
        body = self._call(
            session,
            "GET",
            "/debug_token",
            client_error="invalid_token",
            params={"input_token": access_token},
            headers=bearer(f"{self.app_id}|{self.app_secret}"),
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise OAuthExchangeError("invalid_token", "debug_token without data")
        if str(data.get("app_id", "")) != self.app_id:
            raise OAuthExchangeError("invalid_token", "token issued for another app")
        if data.get("is_valid") is not True:
            raise OAuthExchangeError("invalid_token", "token not valid")

    def _me(self, session: httpx.Client, access_token: str) -> dict[str, Any]:
        return self._call(
            session,
            "GET",
            f"/{self.graph_version}/me",
            client_error="invalid_token",
            params={"fields": "id,email", "appsecret_proof": appsecret_proof(self.app_secret, access_token)},
            headers=bearer(access_token),
        )
