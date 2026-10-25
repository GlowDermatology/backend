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
