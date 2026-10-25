"""``get_oauth_provider(name)``: the fake or live provider for ``google`` / ``facebook``.

``settings.OAUTH_MODE`` (default ``INTEGRATIONS_MODE``) selects the implementation; an unknown
provider name is a 404 so the view can pass the URL kwarg straight through.
"""

from django.conf import settings
from rest_framework.exceptions import NotFound

from .facebook import FacebookOAuthProvider
from .fake import FakeFacebook, FakeGoogle
from .google import GoogleOAuthProvider
from .ports import OAuthProvider

PROVIDER_NAMES = ("google", "facebook")


def get_oauth_provider(name: str) -> OAuthProvider:
    if name not in PROVIDER_NAMES:
        raise NotFound
    if settings.OAUTH_MODE == "fake":
        return FakeGoogle() if name == "google" else FakeFacebook()
    if name == "google":
        return GoogleOAuthProvider(
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            allowed_hd=settings.GOOGLE_ALLOWED_HD,
        )
    return FacebookOAuthProvider(
        app_id=settings.FACEBOOK_APP_ID,
        app_secret=settings.FACEBOOK_APP_SECRET,
        graph_version=settings.FACEBOOK_GRAPH_VERSION,
    )
