"""integrations.readiness: the provider probe behind ``health/?ready=1`` (docs/ops/monitoring.md)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from django.test import override_settings

from glowderma.core import integration_modes, readiness
from glowderma.integrations import readiness as probe_module
from glowderma.integrations.models import IntegrationState

LIVE_BILLZ = {
    "BILLZ_MODE": "live",
    "BILLZ_SECRET_KEY": "billz-integration-key",
    "BILLZ_SHOP_ID": "6b3c0e9a-1d2f-4a5b-8c7d-9e0f1a2b3c4d",
    "BILLZ_CASHBOX_ID": "7c4d1f0b-2e3a-4b6c-9d8e-0f1a2b3c4d5e",
    "BILLZ_PAYMENT_TYPE_ID": "8d5e2a1c-3f4b-4c7d-8e9f-1a2b3c4d5e6f",
}
ALL_LIVE = {f"{name}_MODE": "live" for name in ("OAUTH", "BILLZ", "FCM", "CLICK", "PAYME", "BTS", "ESKIZ")}


def test_the_app_registers_its_probe() -> None:
    assert readiness.probe() is probe_module.PROBE


def test_every_provider_mode_has_a_builder() -> None:
    assert set(probe_module.BUILDERS) == set(integration_modes.PROVIDER_MODE_SETTINGS)


def test_only_live_providers_are_probed() -> None:
    with override_settings(**{name: "fake" for name in ALL_LIVE}):
        assert probe_module.PROBE.configured() == {}


def test_live_adapters_without_their_credentials_are_not_configured() -> None:
    """Every provider has a live adapter (feat/m3-adapters); with the test settings' empty credentials only
    Billz (given its ids here) and OAuth are configured."""
    with override_settings(
        **ALL_LIVE, **{key: value for key, value in LIVE_BILLZ.items() if key != "BILLZ_MODE"}
    ):
        assert probe_module.PROBE.configured() == {
            "oauth": True,
            "billz": True,
            "fcm": False,
            "click": False,
            "payme": False,
            "bts": False,
            "eskiz": False,
        }


LIVE_CREDENTIALS = {
    "CLICK_SERVICE_ID": "12345",
    "CLICK_MERCHANT_ID": "23456",
    "CLICK_MERCHANT_USER_ID": "34567",
    "CLICK_SECRET_KEY": "click-secret",
    "PAYME_MERCHANT_ID": "5e730e8e0b852a417aa49ceb",
    "PAYME_KEY": "payme-key",
    "BTS_LOGIN": "glowderma",
    "BTS_PASSWORD": "bts-password",
    "BTS_SENDER_CITY_CODE": "1",
    "BTS_PICKUP_TYPE": "courier",
    "ESKIZ_EMAIL": "ops@example.uz",
    "ESKIZ_PASSWORD": "eskiz-password",
    "ESKIZ_SENDER": "GlowDerma",
    "FCM_PROJECT_ID": "glowderma-prod",
}


def test_live_adapters_with_their_credentials_are_configured(tmp_path: Path) -> None:
    key = tmp_path / "fcm.json"
    key.write_text("{}", encoding="utf-8")
    settings_ = {**ALL_LIVE, **LIVE_CREDENTIALS, "FCM_CREDENTIALS_FILE": str(key)}
    settings_.update({k: v for k, v in LIVE_BILLZ.items() if k != "BILLZ_MODE"})
    with override_settings(**settings_):
        assert set(probe_module.PROBE.configured().values()) == {True}


@pytest.mark.parametrize(
    ("provider", "change"),
    [
        ("click", {"CLICK_MERCHANT_USER_ID": "00000"}),
        ("click", {"CLICK_SERVICE_ID": "abc"}),
        ("click", {"CLICK_SECRET_KEY": " "}),
        ("bts", {"BTS_PASSWORD": ""}),
        ("bts", {"BTS_SENDER_CITY_CODE": ""}),
        ("bts", {"BTS_PICKUP_TYPE": "branch", "BTS_SENDER_BRANCH_CODE": ""}),
        ("eskiz", {"ESKIZ_SENDER": ""}),
        ("fcm", {"FCM_CREDENTIALS_FILE": "/nonexistent/fcm.json"}),
        ("fcm", {"FCM_PROJECT_ID": ""}),
    ],
)
def test_a_missing_credential_makes_a_live_adapter_unconfigured(
    provider: str, change: dict[str, str], tmp_path: Path
) -> None:
    key = tmp_path / "fcm.json"
    key.write_text("{}", encoding="utf-8")
    base = {**LIVE_CREDENTIALS, "FCM_CREDENTIALS_FILE": str(key), f"{provider.upper()}_MODE": "live"}
    with override_settings(**base):
        assert probe_module.adapter_configured(provider) is True
    with override_settings(**{**base, **change}):
        assert probe_module.adapter_configured(provider) is False


def test_billz_without_its_ids_is_not_configured() -> None:
    with override_settings(**{**LIVE_BILLZ, "BILLZ_CASHBOX_ID": ""}):
        assert probe_module.adapter_configured("billz") is False
    with override_settings(**LIVE_BILLZ):
        assert probe_module.adapter_configured("billz") is True


def test_an_unknown_provider_or_a_failing_check_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refuses:
        def is_configured(self) -> bool:
            return False

    class Raises:
        def is_configured(self) -> bool:
            raise RuntimeError("broken settings")

    assert probe_module.adapter_configured("sms") is False
    monkeypatch.setitem(probe_module.BUILDERS, "click", lambda: [object(), Refuses()])
    assert probe_module.adapter_configured("click") is False
    monkeypatch.setitem(probe_module.BUILDERS, "click", lambda: [Raises()])
    assert probe_module.adapter_configured("click") is False
    monkeypatch.setitem(probe_module.BUILDERS, "click", lambda: [object()])
    assert probe_module.adapter_configured("click") is True  # no is_configured: building it is enough


@pytest.mark.django_db
def test_last_syncs_cover_the_live_syncing_providers() -> None:
    synced = datetime.fromisoformat("2026-10-27T03:07:00+05:00")
    IntegrationState.objects.filter(provider="billz").update(last_sync_at=synced)
    with override_settings(BILLZ_MODE="fake", BTS_MODE="fake"):
        assert probe_module.PROBE.last_syncs() == {}
    with override_settings(BILLZ_MODE="live", BTS_MODE="live"):
        assert probe_module.PROBE.last_syncs() == {"billz": synced, "bts": None}
    with override_settings(BILLZ_MODE="fake", BTS_MODE="live"):
        assert probe_module.PROBE.last_syncs() == {"bts": None}
