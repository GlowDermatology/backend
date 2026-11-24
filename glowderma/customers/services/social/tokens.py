"""The checks of a social token (customer contract §2.1.8): the (provider, token type) pairs, the single-use
nonce, the provider verification and the Apple code exchange."""

from __future__ import annotations

from django.conf import settings

from glowderma.accounts import services as account_services
from glowderma.core import enums, integration_modes
from glowderma.core.api.exceptions import (
    ProviderUnavailable,
    SocialTokenInvalid,
)
from glowderma.customers.services.common import passwords
from glowderma.integrations.oauth import registry as oauth_registry
from glowderma.integrations.oauth.ports import AppTokenClaims, OAuthExchangeError

PROVIDER_LABELS = {"google": "Google", "facebook": "Facebook", "apple": "Apple"}


DEFAULT_TOKEN_TYPES: dict[str, str] = {
    enums.CustomerSocialProvider.GOOGLE: enums.SocialTokenType.ID_TOKEN,
    enums.CustomerSocialProvider.APPLE: enums.SocialTokenType.ID_TOKEN,
    enums.CustomerSocialProvider.FACEBOOK: enums.SocialTokenType.ACCESS_TOKEN,
}


# Customer contract §2.1.8: only these four (provider, token type) pairs exist.
TOKEN_PAIRS = frozenset(
    {
        ("google", "id_token"),
        ("apple", "id_token"),
        ("facebook", "access_token"),
        ("facebook", "id_token"),
    }
)


NONCE_WILDCARD = "*"


def _nonce_accepted(nonce: str | None, fid: str) -> bool:
    if nonce == NONCE_WILDCARD and integration_modes.dev_features_allowed() and settings.OAUTH_MODE == "fake":
        return True
    return bool(nonce) and account_services.consume_social_nonce(str(nonce), fid)


def verify_social_token(
    provider: str, token: str, token_type: str | None, nonce: str | None, *, fid: str
) -> AppTokenClaims:
    """The checks of customer contract §2.1.8: availability (404), the four (provider, token type) pairs, the
    single-use nonce bound to this install (ID tokens), the provider verification and the replay cache."""
    verifier = oauth_registry.get_app_verifier(provider)
    kind = token_type or DEFAULT_TOKEN_TYPES[provider]
    if (provider, kind) not in TOKEN_PAIRS:
        raise SocialTokenInvalid()
    if kind == enums.SocialTokenType.ID_TOKEN and not _nonce_accepted(nonce, fid):
        raise SocialTokenInvalid()
    try:
        claims = verifier.verify_app_token(token, kind, nonce)
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise ProviderUnavailable() from exc
        raise SocialTokenInvalid() from exc
    if kind == enums.SocialTokenType.ID_TOKEN and not account_services.remember_id_token(
        provider, token, expires_at=claims.expires_at
    ):
        raise SocialTokenInvalid()
    return claims


def exchange_apple_code(code: str | None, claims: AppTokenClaims) -> tuple[str, str]:
    """Exchange Apple's authorization code before any row lock; ``(sealed refresh token, client_id)``."""
    if not code:
        raise passwords.field_refusal("authorization_code", "required")
    client_id = claims.audience
    # B-W1: a code of the website's Sign in with Apple JS (its ID token's aud is a Services ID) is exchanged
    # with the redirect URI the authorization used; the app's code has none.
    redirect_uri = settings.APPLE_WEB_REDIRECT_URI if client_id in settings.APPLE_SERVICES_IDS else None
    try:
        refresh = oauth_registry.get_apple_service().exchange_code(
            code, client_id=client_id, subject=claims.subject, redirect_uri=redirect_uri or None
        )
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise ProviderUnavailable() from exc
        raise SocialTokenInvalid() from exc
    return account_services.seal_apple_refresh(refresh, client_id=client_id), client_id


__all__ = [
    "DEFAULT_TOKEN_TYPES",
    "NONCE_WILDCARD",
    "PROVIDER_LABELS",
    "TOKEN_PAIRS",
    "exchange_apple_code",
    "verify_social_token",
]
