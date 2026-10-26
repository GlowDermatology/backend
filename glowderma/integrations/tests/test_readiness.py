"""integrations.readiness: the provider probe behind ``health/?ready=1`` (docs/ops/monitoring.md)."""

from __future__ import annotations

from datetime import datetime

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


def test_live_adapters_that_do_not_exist_yet_are_not_configured() -> None:
    """On this branch only Billz and OAuth have live adapters; Click, Payme, BTS, Eskiz and FCM arrive with
    feat/m3-adapters (their registries raise NotImplementedError / ImproperlyConfigured until then)."""
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
