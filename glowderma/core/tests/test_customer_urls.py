"""URL mounts and beat entries of the customer API (customer contract §1.1, plan §1 P0, §8)."""

import importlib
from collections.abc import Iterator

import pytest
from django.test import override_settings
from django.urls import clear_url_caches

import config.urls
from config.celery import app


def mounted(module: object) -> set[str]:
    names: set[str] = set()
    for pattern in module.urlpatterns:  # type: ignore[attr-defined]
        urlconf = getattr(pattern, "urlconf_name", None)
        name = getattr(urlconf, "__name__", urlconf)
        if isinstance(name, str):
            names.add(f"{pattern.pattern}|{name}")
    return names


@pytest.fixture
def reload_urls() -> Iterator[object]:
    try:
        yield importlib.reload
    finally:
        importlib.reload(config.urls)
        clear_url_caches()


def test_every_customer_module_is_mounted_at_api_v1() -> None:
    names = mounted(config.urls)
    assert config.urls.CUSTOMER_API_MODULES == (
        "customers",
        "site_config",
        "delivery",
        "commerce",
        "loyalty",
        "appointments",
        "notifications",
    )
    for module in config.urls.CUSTOMER_API_MODULES:
        assert f"api/v1/|glowderma.{module}.api.customer.urls" in names
    assert "api/v1/webhooks/|glowderma.commerce.api.webhooks.urls" in names
    assert "api/v1/pos/|glowderma.loyalty.api.pos.urls" in names
    assert not any("glowderma.commerce.api.dev.urls" in name for name in names)  # FAKE_PAYMENTS_ENABLED off


@pytest.mark.parametrize(
    ("overrides", "enabled"),
    [
        ({"FAKE_PAYMENTS_ENABLED": True}, True),
        ({"FAKE_PAYMENTS_ENABLED": False}, False),
        ({"FAKE_PAYMENTS_ENABLED": True, "DEV_FEATURES_ALLOWED": False}, False),
        ({"FAKE_PAYMENTS_ENABLED": True, "CLICK_MODE": "live"}, False),
        ({"FAKE_PAYMENTS_ENABLED": True, "PAYME_MODE": "live"}, False),
    ],
)
def test_dev_routes_need_dev_features_fake_payments_and_fake_providers(
    overrides: dict[str, object], enabled: bool
) -> None:
    with override_settings(**overrides):
        assert config.urls.dev_routes_enabled() is enabled


def test_dev_routes_are_mounted_when_enabled(reload_urls: object) -> None:
    with override_settings(FAKE_PAYMENTS_ENABLED=True):
        module = importlib.reload(config.urls)
        assert "api/v1/dev/|glowderma.commerce.api.dev.urls" in mounted(module)


def test_beat_runs_the_auth_cleanup_and_the_bts_poll() -> None:
    schedule = app.conf.beat_schedule
    assert schedule["accounts-cleanup-auth"] == {"task": "accounts.cleanup_auth", "schedule": 3600.0}
    assert schedule["commerce-poll-bts"] == {"task": "commerce.poll_bts", "schedule": 600.0}
    assert not any(entry["task"] == "loyalty.expire_holds" for entry in schedule.values())
