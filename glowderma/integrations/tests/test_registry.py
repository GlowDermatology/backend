import pytest
from django.test import override_settings
from rest_framework.exceptions import NotFound

from glowderma.integrations.oauth.facebook import FacebookOAuthProvider
from glowderma.integrations.oauth.fake import FakeFacebook, FakeGoogle
from glowderma.integrations.oauth.google import GoogleOAuthProvider
from glowderma.integrations.oauth.registry import PROVIDER_NAMES, get_oauth_provider


def test_provider_names_match_contract() -> None:
    assert PROVIDER_NAMES == ("google", "facebook")


@pytest.mark.parametrize("name", ["github", "Google", "", "google/"])
def test_unknown_provider_is_not_found(name: str) -> None:
    with pytest.raises(NotFound):
        get_oauth_provider(name)


def test_fake_mode_is_the_test_default() -> None:
    assert isinstance(get_oauth_provider("google"), FakeGoogle)
    assert isinstance(get_oauth_provider("facebook"), FakeFacebook)


@override_settings(
    OAUTH_MODE="live",
    GOOGLE_OAUTH_CLIENT_ID="cid.apps.googleusercontent.com",
    GOOGLE_OAUTH_CLIENT_SECRET="gsecret",  # noqa: S106
    GOOGLE_ALLOWED_HD="glowderma.uz",
    FACEBOOK_APP_ID="fbid",
    FACEBOOK_APP_SECRET="fbsecret",  # noqa: S106
    FACEBOOK_GRAPH_VERSION="v22.0",
)
def test_live_mode_builds_adapters_from_settings() -> None:
    google = get_oauth_provider("google")
    assert isinstance(google, GoogleOAuthProvider)
    assert (google.client_id, google.client_secret, google.allowed_hd) == (
        "cid.apps.googleusercontent.com",
        "gsecret",
        "glowderma.uz",
    )
    facebook = get_oauth_provider("facebook")
    assert isinstance(facebook, FacebookOAuthProvider)
    assert (facebook.app_id, facebook.app_secret, facebook.graph_version) == ("fbid", "fbsecret", "v22.0")
