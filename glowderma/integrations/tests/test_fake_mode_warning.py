"""Fake providers are allowed in production (product owner decision 2026-10-26): one startup WARNING line
(logger ``glowderma.integrations``) lists them, from ``IntegrationsConfig.ready`` — never a system check."""

import logging

import pytest
from django.apps import apps as django_apps
from django.core import checks
from django.test import override_settings

from glowderma.core import integration_modes
from glowderma.integrations import apps as integrations_apps

PRODUCTION = integration_modes.PRODUCTION_SETTINGS_MODULE
ALL_FAKE = {f"{name}_MODE": "fake" for name in ("OAUTH", "BILLZ", "FCM", "CLICK", "PAYME", "BTS")}


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations_apps, "_fake_mode_warned", False)


def _lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "glowderma.integrations"]


def test_production_warns_once_per_process_listing_the_fake_providers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        override_settings(
            SETTINGS_MODULE=PRODUCTION, **{**ALL_FAKE, "BTS_MODE": "live", "OAUTH_MODE": "live"}
        ),
        caplog.at_level(logging.WARNING, logger="glowderma.integrations"),
    ):
        assert integrations_apps.warn_fake_providers() is True
        assert integrations_apps.warn_fake_providers() is False
        django_apps.get_app_config("integrations").ready()  # a second ready() in the process stays silent
    assert _lines(caplog) == [
        ("WARNING", "integrations running in fake mode in production: billz, fcm, click, payme")
    ]


def test_no_warning_outside_production_or_without_fakes(caplog: pytest.LogCaptureFixture) -> None:
    all_live = dict.fromkeys(ALL_FAKE, "live")
    with caplog.at_level(logging.WARNING, logger="glowderma.integrations"):
        with override_settings(SETTINGS_MODULE="config.settings.local", **ALL_FAKE):
            assert integrations_apps.warn_fake_providers() is False
        with override_settings(SETTINGS_MODULE=PRODUCTION, **all_live):
            assert integrations_apps.warn_fake_providers() is False
    assert _lines(caplog) == []


def test_ready_emits_the_warning_and_no_system_check_reports_fake_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        override_settings(SETTINGS_MODULE=PRODUCTION, **ALL_FAKE),
        caplog.at_level(logging.WARNING, logger="glowderma.integrations"),
    ):
        django_apps.get_app_config("integrations").ready()
        messages = checks.run_checks()
    assert _lines(caplog) == [
        ("WARNING", "integrations running in fake mode in production: oauth, billz, fcm, click, payme, bts")
    ]
    assert not [m for m in messages if "fake" in str(m.msg).lower()]


def test_provider_modes_cover_every_mode_setting() -> None:
    with override_settings(**{**ALL_FAKE, "PAYME_MODE": "live"}):
        assert integration_modes.provider_modes() == {
            "oauth": "fake",
            "billz": "fake",
            "fcm": "fake",
            "click": "fake",
            "payme": "live",
            "bts": "fake",
        }
        assert integration_modes.fake_providers() == ["oauth", "billz", "fcm", "click", "bts"]
