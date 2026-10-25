"""Deterministic OAuth providers for tests and local end-to-end runs (``OAUTH_MODE=fake``).

The authorization ``code`` encodes the outcome, so no network and no browser flow are needed:

``fake:<subject>:<email>[:<hd>]``
    Success with a verified email (``hd`` optional, e.g. ``fake:42:ali@glowderma.uz:glowderma.uz``).
``fake-unverified:<subject>:<email>[:<hd>]``
    Success, but the provider does not vouch for the email (``email_verified`` is ``False``).
``fake-fail``
    The provider rejects the code (``exchange_failed``, like an expired or reused code).
``fake-down``
    The provider does not answer (``unavailable``).

Any other code is ``exchange_failed``. ``code_verifier`` must be 43..128 characters (PKCE), otherwise
``exchange_failed``. ``redirect_uri`` is accepted as given (the view already compared it with
``OAUTH_REDIRECT_URI``).

:class:`FakeGoogle` checks the nonce like the live adapter does: it must be ``nonce-<subject>`` or the
wildcard ``*`` (any other value is ``invalid_token``). :class:`FakeFacebook` ignores the nonce and, like
the live adapter, never reports a verified email or an ``hd``.
"""

from .ports import OAuthExchangeError, SocialClaims

CODE_VERIFIER_MIN = 43
CODE_VERIFIER_MAX = 128
NONCE_WILDCARD = "*"


def _parse_code(code: str) -> tuple[str, str, str | None, bool]:
    """``(subject, email, hd, email_verified)`` for a fake code, or the failure it encodes."""
    if code == "fake-fail":
        raise OAuthExchangeError("exchange_failed", "fake code rejected")
    if code == "fake-down":
        raise OAuthExchangeError("unavailable", "fake provider down")
    parts = code.split(":")
    if parts[0] not in {"fake", "fake-unverified"} or len(parts) not in {3, 4} or not parts[1]:
        raise OAuthExchangeError("exchange_failed", "unknown fake code")
    hd = parts[3] if len(parts) == 4 and parts[3] else None
    return parts[1], parts[2], hd, parts[0] == "fake"


def _check_verifier(code_verifier: str) -> None:
    if not CODE_VERIFIER_MIN <= len(code_verifier) <= CODE_VERIFIER_MAX:
        raise OAuthExchangeError("exchange_failed", "code_verifier length")


class FakeGoogle:
    name = "google"

    def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> SocialClaims:
        _check_verifier(code_verifier)
        subject, email, hd, verified = _parse_code(code)
        if nonce != NONCE_WILDCARD and nonce != f"nonce-{subject}":
            raise OAuthExchangeError("invalid_token", "nonce mismatch")
        return SocialClaims(provider=self.name, subject=subject, email=email, email_verified=verified, hd=hd)


class FakeFacebook:
    name = "facebook"

    def exchange(
        self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str | None
    ) -> SocialClaims:
        _check_verifier(code_verifier)
        subject, email, _hd, _verified = _parse_code(code)
        return SocialClaims(provider=self.name, subject=subject, email=email, email_verified=False, hd=None)
