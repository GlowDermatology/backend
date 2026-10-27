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

from django.conf import settings

from glowderma.core import integration_modes, timeutils

from .ports import AppTokenClaims, OAuthExchangeError, SocialClaims

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


# --- customer app tokens (customer contract §2.1.8; plan §2 P1 "Fakes", §6) --------------------------------
#
# ``fake:<subject>:<email>`` / ``fake-unverified:<subject>:<email>`` (Google, Facebook) and
# ``fake-apple:<subject>:<email>[:<bundle id>]`` (Apple; relay addresses allowed) are valid tokens;
# ``fake-fail``
# → ``exchange_failed``, ``fake-down`` → ``unavailable``, ``fake-expired`` → ``invalid_token``; anything else
# is ``invalid_token``. The nonce is not part of a fake token: the customer service checks that it was issued
# (and the ``*`` wildcard only under the local/test settings). Apple's authorization code ``fake-code:<any>``
# yields the refresh token ``fake-apple-refresh-<subject>``, ``fake-code-sub:<subject>`` the same but only for
# that subject (another identity → refused, as the live adapter's id_token ``sub`` check), ``fake-code-fail``
# is refused, ``fake-code-down`` unavailable. The fakes never run under the production settings: the registry
# answers 404 first.

FAKE_TOKEN_LIFETIME_SECONDS = 600
FAKE_GOOGLE_CLIENT_ID = "fake-google-client"
FAKE_APPLE_BUNDLE_ID = "uz.glowderma.app"
FAKE_CODE_SUBJECT_PREFIX = "fake-code-sub:"


def _now() -> int:
    return int(timeutils.now().timestamp())


def _parse_app_token(token: str, prefixes: frozenset[str]) -> tuple[str, str, str]:
    """``(subject, email, extra)`` of a fake app token with one of ``prefixes``, or the failure it encodes."""
    if token == "fake-fail":  # noqa: S105 - a fake outcome literal
        raise OAuthExchangeError("exchange_failed", "fake token rejected")
    if token == "fake-down":  # noqa: S105 - a fake outcome literal
        raise OAuthExchangeError("unavailable", "fake provider down")
    if token == "fake-expired":  # noqa: S105 - a fake outcome literal
        raise OAuthExchangeError("invalid_token", "fake token expired")
    parts = token.split(":")
    if parts[0] not in prefixes or len(parts) not in {3, 4} or not parts[1]:
        raise OAuthExchangeError("invalid_token", "unknown fake token")
    return parts[1], parts[2], parts[3] if len(parts) == 4 else ""


def _first(values: object, default: str) -> str:
    items = [str(value) for value in values] if isinstance(values, (list, tuple)) else []
    return items[0] if items else default


class FakeGoogleApp:
    name = "google"

    def verify_app_token(self, token: str, token_type: str, nonce: str | None) -> AppTokenClaims:
        subject, email, _ = _parse_app_token(token, frozenset({"fake", "fake-unverified"}))
        audience = _first(getattr(settings, "GOOGLE_APP_CLIENT_IDS", []), FAKE_GOOGLE_CLIENT_ID)
        return AppTokenClaims(self.name, subject, email, audience, _now() + FAKE_TOKEN_LIFETIME_SECONDS)


class FakeFacebookApp:
    name = "facebook"

    def verify_app_token(self, token: str, token_type: str, nonce: str | None) -> AppTokenClaims:
        subject, email, _ = _parse_app_token(token, frozenset({"fake", "fake-unverified"}))
        audience = str(getattr(settings, "FACEBOOK_APP_ID", "")) or "fake-facebook-app"
        return AppTokenClaims(self.name, subject, email, audience, _now() + FAKE_TOKEN_LIFETIME_SECONDS)


class FakeApple:
    """The fake Sign in with Apple: token verification, code exchange and revocation.

    ``revoked`` keeps ``(refresh_token, client_id)`` of every revocation **only while**
    ``settings.DEV_FEATURES_ALLOWED`` (local/test); otherwise nothing is kept.
    """

    name = "apple"

    def __init__(self) -> None:
        self.revoked: list[tuple[str, str]] = []
        self.exchanged: list[tuple[str, str]] = []

    def verify_app_token(self, token: str, token_type: str, nonce: str | None) -> AppTokenClaims:
        subject, email, bundle_id = _parse_app_token(token, frozenset({"fake-apple"}))
        configured = [str(value) for value in getattr(settings, "APPLE_BUNDLE_IDS", [])]
        audience = bundle_id or _first(configured, FAKE_APPLE_BUNDLE_ID)
        if configured and audience not in configured:
            raise OAuthExchangeError("invalid_token", "aud is not a configured bundle id")
        return AppTokenClaims(self.name, subject, email, audience, _now() + FAKE_TOKEN_LIFETIME_SECONDS)

    def exchange_code(self, code: str, *, client_id: str, subject: str) -> str:
        if code == "fake-code-fail":
            raise OAuthExchangeError("exchange_failed", "fake code refused")
        if code == "fake-code-down":
            raise OAuthExchangeError("unavailable", "fake Apple down")
        if code.startswith(FAKE_CODE_SUBJECT_PREFIX):
            # A code bound to an Apple account: refused for any other identity, like the live adapter's
            # id_token ``sub`` check.
            if code.removeprefix(FAKE_CODE_SUBJECT_PREFIX) != subject:
                raise OAuthExchangeError("exchange_failed", "authorization code of another Apple account")
        elif not code.startswith("fake-code:"):
            raise OAuthExchangeError("exchange_failed", "unknown fake code")
        if _records():
            self.exchanged.append((code, client_id))
        return f"fake-apple-refresh-{subject}"

    def revoke(self, refresh_token: str, *, client_id: str) -> None:
        if refresh_token.endswith("-down"):
            raise OAuthExchangeError("unavailable", "fake Apple down")
        if refresh_token.endswith("-invalid"):
            raise OAuthExchangeError("exchange_failed", "token already invalid")
        if _records():
            self.revoked.append((refresh_token, client_id))


def _records() -> bool:
    return integration_modes.dev_features_allowed()
