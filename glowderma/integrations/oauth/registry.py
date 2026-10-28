"""OAuth provider lookup for the staff routes and the customer app (admin contract §2.1, customer contract
§2.1.8).

* :func:`get_oauth_provider` (staff ``auth/oauth/{provider}/``): ``google`` / ``facebook`` only
  (``PROVIDER_NAMES`` = ``STAFF_PROVIDERS``; ``apple`` never reaches the staff routes).
* :func:`get_app_verifier` (customer ``auth/social/``, ``me/social/``): ``google`` / ``facebook`` / ``apple``
  (``CUSTOMER_PROVIDERS``); ``apple`` additionally needs ``APPLE_BUNDLE_IDS`` or ``APPLE_SERVICES_IDS``
  (the website, B-W1) with ``OAUTH_MODE=live`` (both empty → 404), while the fake Apple adapter answers
  with ``OAUTH_MODE=fake`` (local/test) even without them. Google accepts ``GOOGLE_APP_CLIENT_IDS`` and
  ``GOOGLE_WEB_CLIENT_IDS``.
* :func:`get_apple_service`: the Apple code exchange and revocation (fake or live).

``settings.OAUTH_MODE`` (default ``INTEGRATIONS_MODE``) selects the implementation; an unknown provider
name is
a 404 so the views pass the URL kwarg straight through. **While OAuth is fake under the production settings**
(``core.integration_modes.fake_in_production("oauth")``) every lookup — staff and customer — raises
``NotFound``: a fake verifier accepts made-up subjects, which would sign anyone in as any linked identity
(customer contract §6.14). Only the revocation handler bypasses the gate (``require_available=False``).
"""

from django.conf import settings
from rest_framework.exceptions import NotFound

from glowderma.core import integration_modes

from .apple import AppleProvider
from .facebook import FacebookOAuthProvider
from .fake import FakeApple, FakeFacebook, FakeFacebookApp, FakeGoogle, FakeGoogleApp
from .google import GoogleAppTokenVerifier, GoogleOAuthProvider
from .ports import AppleTokenService, AppTokenVerifier, OAuthProvider

PROVIDER_NAMES = ("google", "facebook")
STAFF_PROVIDERS = PROVIDER_NAMES
CUSTOMER_PROVIDERS = ("google", "facebook", "apple")

# One fake Apple per process, so tests (and local runs) can read the recorded revocations.
_fake_apple = FakeApple()


def _refuse_fake_in_production() -> None:
    if integration_modes.fake_in_production("oauth"):
        raise NotFound


def get_oauth_provider(name: str) -> OAuthProvider:
    if name not in STAFF_PROVIDERS:
        raise NotFound
    _refuse_fake_in_production()
    if settings.OAUTH_MODE == "fake":
        return FakeGoogle() if name == "google" else FakeFacebook()
    if name == "google":
        return GoogleOAuthProvider(
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            allowed_hd=settings.GOOGLE_ALLOWED_HD,
        )
    return _facebook()


def _facebook() -> FacebookOAuthProvider:
    return FacebookOAuthProvider(
        app_id=settings.FACEBOOK_APP_ID,
        app_secret=settings.FACEBOOK_APP_SECRET,
        graph_version=settings.FACEBOOK_GRAPH_VERSION,
    )


def _apple() -> AppleProvider:
    return AppleProvider(
        bundle_ids=list(settings.APPLE_BUNDLE_IDS),
        services_ids=list(settings.APPLE_SERVICES_IDS),
        team_id=settings.APPLE_TEAM_ID,
        key_id=settings.APPLE_KEY_ID,
        private_key_file=settings.APPLE_PRIVATE_KEY_FILE,
    )


def customer_provider_available(name: str) -> bool:
    """Whether customer social sign-in with ``name`` is offered (else the routes answer 404)."""
    if name not in CUSTOMER_PROVIDERS or integration_modes.fake_in_production("oauth"):
        return False
    return not (
        name == "apple"
        and settings.OAUTH_MODE == "live"
        and not settings.APPLE_BUNDLE_IDS
        and not settings.APPLE_SERVICES_IDS
    )


def get_app_verifier(name: str) -> AppTokenVerifier:
    """The verifier of customer app tokens for ``name`` (404 when the provider is not offered)."""
    if not customer_provider_available(name):
        raise NotFound
    if settings.OAUTH_MODE == "fake":
        if name == "apple":
            return _fake_apple
        return FakeGoogleApp() if name == "google" else FakeFacebookApp()
    if name == "google":
        # B-W1: the website's Google Identity Services client ids are accepted next to the app's.
        return GoogleAppTokenVerifier(
            client_ids=list(dict.fromkeys([*settings.GOOGLE_APP_CLIENT_IDS, *settings.GOOGLE_WEB_CLIENT_IDS]))
        )
    if name == "facebook":
        return _facebook()
    return _apple()


def get_apple_service(*, require_available: bool = True) -> AppleTokenService:
    """Apple's code exchange and revocation. The sign-in paths keep the 404 gate; the ``apple.revoke``
    handler passes ``require_available=False`` so tokens stored earlier are still revoked."""
    if require_available and not customer_provider_available("apple"):
        raise NotFound
    if settings.OAUTH_MODE == "fake":
        return _fake_apple
    return _apple()


def fake_apple() -> FakeApple:
    """The process-wide fake Apple (tests read ``revoked`` / ``exchanged``)."""
    return _fake_apple
