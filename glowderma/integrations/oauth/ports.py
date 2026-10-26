from dataclasses import dataclass
from typing import Literal, Protocol

type OAuthErrorKind = Literal["exchange_failed", "unavailable", "invalid_token"]


@dataclass(frozen=True, slots=True)
class SocialClaims:
    """What a provider asserts about the person who completed the browser flow.

    ``email_verified`` is only meaningful for Google (Facebook never asserts it); ``hd`` is the Google
    Workspace domain of the account, ``None`` for consumer accounts and for Facebook.
    """

    provider: str
    subject: str
    email: str
    email_verified: bool
    hd: str | None


class OAuthExchangeError(Exception):
    """The authorization code could not be turned into verified claims.

    ``kind`` says why: ``exchange_failed`` (bad, expired or reused code, verifier mismatch, wrong
    redirect URI), ``unavailable`` (the provider did not answer or answered 5xx) or ``invalid_token``
    (the provider answered but the token it returned fails verification).
    """

    def __init__(self, kind: OAuthErrorKind, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind: OAuthErrorKind = kind
        self.detail = detail


class OAuthProvider(Protocol):
    name: str

    def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> SocialClaims: ...


# --- customer app tokens (customer contract §2.1.8, M2 P1) --------------------------------------------------
# The app signs in with the provider's SDK and sends us the token; the server verifies it (no code exchange,
# except Apple's authorization code, which yields the refresh token revoked on unlink and deletion).

ID_TOKEN = "id_token"  # noqa: S105 - a token type literal
ACCESS_TOKEN = "access_token"  # noqa: S105 - a token type literal


@dataclass(frozen=True, slots=True)
class AppTokenClaims:
    """What a provider asserts in a token the customer app obtained (customer contract §2.1.8).

    ``email`` is optional (``""``: the phone is the identity; Apple may send a private relay address);
    ``audience`` is the client the token was issued for (Apple: the bundle id, the ``client_id`` of the code
    exchange and the revocation); ``expires_at`` (epoch seconds) bounds the replay cache entry.
    """

    provider: str
    subject: str
    email: str
    audience: str
    expires_at: int


class AppTokenVerifier(Protocol):
    name: str

    def verify_app_token(self, token: str, token_type: str, nonce: str | None) -> AppTokenClaims:
        """Verify ``token`` of ``token_type`` (the caller already refused unsupported pairs); raises
        :class:`OAuthExchangeError` (``invalid_token`` / ``exchange_failed`` / ``unavailable``)."""
        ...


class AppleTokenService(Protocol):
    """Sign in with Apple's server calls (customer contract §2.1.8): the authorization-code exchange and the
    refresh-token revocation, both authenticated by an ES256 client secret."""

    def exchange_code(self, code: str, *, client_id: str, subject: str) -> str:
        """Apple's refresh token for ``code`` (``exchange_failed`` when Apple refuses, ``unavailable``)."""
        ...

    def revoke(self, refresh_token: str, *, client_id: str) -> None:
        """Revoke ``refresh_token`` (``exchange_failed`` = Apple answered 4xx, final; ``unavailable``)."""
        ...
