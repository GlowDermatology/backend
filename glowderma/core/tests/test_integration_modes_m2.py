"""The fake-provider predicates of customer contract §6.14 over (mode, ``DEV_FEATURES_ALLOWED``)."""

import pytest
from django.test import override_settings

from glowderma.core import integration_modes

ALL = ("OAUTH", "BILLZ", "FCM", "CLICK", "PAYME", "BTS", "ESKIZ")


def modes(mode: str, **overrides: str) -> dict[str, str]:
    return {**{f"{name}_MODE": mode for name in ALL}, **overrides}


def test_eskiz_is_reported_with_the_other_providers() -> None:
    assert integration_modes.PROVIDER_MODE_SETTINGS["eskiz"] == "ESKIZ_MODE"
    with override_settings(**modes("fake", ESKIZ_MODE="live")):
        assert integration_modes.provider_modes()["eskiz"] == "live"
        assert "eskiz" not in integration_modes.fake_providers()


@pytest.mark.parametrize(
    ("mode", "dev", "fake_in_production"),
    [("fake", True, False), ("fake", False, True), ("live", True, False), ("live", False, False)],
)
def test_fake_in_production_matrix(mode: str, dev: bool, fake_in_production: bool) -> None:
    with override_settings(**modes(mode), DEV_FEATURES_ALLOWED=dev):
        for provider in integration_modes.PROVIDER_MODE_SETTINGS:
            assert integration_modes.fake_in_production(provider) is fake_in_production
        for provider in ("click", "payme", "bts"):
            assert integration_modes.callbacks_refused(provider) is fake_in_production
        assert integration_modes.payable_providers() == ([] if fake_in_production else ["click", "payme"])
        assert integration_modes.delivery_available() is not fake_in_production
        assert integration_modes.fulfilment_methods() == (
            ["pickup"] if fake_in_production else ["delivery", "pickup"]
        )
        assert integration_modes.online_sales_enabled() is not fake_in_production


def test_predicates_are_per_provider_when_modes_are_mixed() -> None:
    with override_settings(**modes("live", PAYME_MODE="fake", BILLZ_MODE="fake"), DEV_FEATURES_ALLOWED=False):
        assert integration_modes.payable_providers() == ["click"]
        assert integration_modes.delivery_available() is True
        assert integration_modes.online_sales_enabled() is False
    with override_settings(**modes("live", CLICK_MODE="fake", BTS_MODE="fake"), DEV_FEATURES_ALLOWED=False):
        assert integration_modes.payable_providers() == ["payme"]
        assert integration_modes.fulfilment_methods() == ["pickup"]
        assert integration_modes.online_sales_enabled() is True


def test_unknown_providers_are_programming_errors() -> None:
    with pytest.raises(KeyError):
        integration_modes.fake_in_production("sms")
    with pytest.raises(KeyError):
        integration_modes.callbacks_refused("billz")


def test_test_settings_allow_dev_features() -> None:
    assert integration_modes.dev_features_allowed() is True
    with override_settings(DEV_FEATURES_ALLOWED=False):
        assert integration_modes.dev_features_allowed() is False
