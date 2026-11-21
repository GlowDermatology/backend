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
    ``trusted_email`` says the provider vouches that the signer owns ``email`` (Google only: see
    :func:`google_email_trusted`); a trusted address may match the account of the same address.
    """

    provider: str
    subject: str
    email: str
    audience: str
    expires_at: int
    trusted_email: bool = False


GOOGLE_CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def google_email_trusted(email: object, email_verified: object, hd: object) -> bool:
    """Google's rule for using an ID token's address as an identity (Sign in with Google: authenticate with a
    backend server): ``email_verified`` must be ``true`` and the address a Gmail one or one of the token's
    Google Workspace domain (``hd``). Any other verified address only proves it was verified once, not that
    the signer still holds the mailbox."""
    if email_verified is not True or not isinstance(email, str) or email.count("@") != 1:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    if domain in GOOGLE_CONSUMER_DOMAINS:
        return True
    return isinstance(hd, str) and bool(hd) and domain == hd.lower()


class AppTokenVerifier(Protocol):
    name: str

    def verify_app_token(self, token: str, token_type: str, nonce: str | None) -> AppTokenClaims:
        """Verify ``token`` of ``token_type`` (the caller already refused unsupported pairs); raises
        :class:`OAuthExchangeError` (``invalid_token`` / ``exchange_failed`` / ``unavailable``)."""
        ...


class AppleTokenService(Protocol):
    """Sign in with Apple's server calls (customer contract §2.1.8): the authorization-code exchange and the
    refresh-token revocation, both authenticated by an ES256 client secret."""

    def exchange_code(
        self, code: str, *, client_id: str, subject: str, redirect_uri: str | None = None
    ) -> str:
        """Apple's refresh token for ``code`` (``exchange_failed`` when Apple refuses, ``unavailable``).
        ``redirect_uri``: the website's Sign in with Apple JS flow (a Services ID ``client_id``) must send the
        redirect URI its authorization used; the native app's code has none (B-W1)."""
        ...

    def revoke(self, refresh_token: str, *, client_id: str) -> None:
        """Revoke ``refresh_token`` (``exchange_failed`` = Apple answered 4xx, final; ``unavailable``)."""
        ...
