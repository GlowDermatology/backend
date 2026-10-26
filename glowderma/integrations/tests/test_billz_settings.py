"""``BILLZ_*`` settings: the fake needs none; ``BILLZ_MODE=live`` refuses to start without every value."""

# ruff: noqa: F811 - the settings fixtures are imported from the core settings tests and requested by name

from collections.abc import Callable
from types import ModuleType

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - shared fixtures
    load_fresh_production,
    production_env,
)
from glowderma.core.tests.test_settings_m2 import load_fresh_local  # noqa: F401 - shared fixture

LIVE_ENV = {
    "BILLZ_MODE": "live",
    "BILLZ_SECRET_KEY": "billz-issued-integration-key",
    "BILLZ_SHOP_ID": "c1056fcc-539b-47ea-8bc4-d9e337c433f0",
    "BILLZ_CASHBOX_ID": "9b4a7f8c-a8ef-4870-8a1b-89869fdd4141",
    "BILLZ_PAYMENT_TYPE_ID": "c6e84d9b-0ef0-4d5a-886d-147c74a9b051",
}


def test_test_settings_default_to_the_fake_with_documented_values() -> None:
    assert settings.BILLZ_MODE == "fake"
    assert settings.BILLZ_BASE_URL == "https://api-admin.billz.ai"
    assert settings.BILLZ_PLATFORM_ID == "7d4a4c38-dd84-4902-b744-0488b80a4c01"
    assert (settings.BILLZ_SECRET_KEY, settings.BILLZ_SHOP_ID) == ("", "")
    assert (
        settings.BILLZ_RATE_LIMIT_PER_SECOND == 1.5 and settings.BILLZ_PAGE_SIZE == 300
    )  # below Billz's 2/s
    assert (settings.BILLZ_SHIFT_RETRY_MINUTES, settings.BILLZ_SHIFT_RETRY_MAX_HOURS) == (15, 36)
    assert settings.BILLZ_FULL_SYNC_MIN_RATIO == 0.8 and settings.BILLZ_VOLUME_FIELD == ""
    assert (settings.BILLZ_CONNECT_TIMEOUT_SECONDS, settings.BILLZ_READ_TIMEOUT_SECONDS) == (5.0, 30.0)
    assert settings.LOGGING["loggers"]["httpx"] == {"level": "WARNING"}  # request URLs can carry phones


def test_fake_billz_in_production_needs_no_billz_variable(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (*LIVE_ENV, "INTEGRATIONS_MODE"):
        monkeypatch.delenv(name, raising=False)
    production = load_fresh_production()
    assert production.BILLZ_MODE == "fake" and production.BILLZ_SECRET_KEY == ""


def test_integrations_mode_live_makes_billz_live_and_demands_its_values(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTEGRATIONS_MODE", "live")
    # the other live adapters would demand their own values first (integrations/tests/test_live_settings.py)
    for name in ("OAUTH_MODE", "CLICK_MODE", "PAYME_MODE", "BTS_MODE", "ESKIZ_MODE", "FCM_MODE"):
        monkeypatch.setenv(name, "fake")
    monkeypatch.delenv("BILLZ_MODE", raising=False)
    with pytest.raises(ImproperlyConfigured, match="BILLZ_SECRET_KEY is required"):
        load_fresh_production()


def test_live_billz_in_production_starts_with_every_value(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in LIVE_ENV.items():
        monkeypatch.setenv(name, value)
    production = load_fresh_production()
    assert production.BILLZ_MODE == "live" and production.BILLZ_SHOP_ID == LIVE_ENV["BILLZ_SHOP_ID"]


@pytest.mark.parametrize(
    "missing", ["BILLZ_SECRET_KEY", "BILLZ_SHOP_ID", "BILLZ_CASHBOX_ID", "BILLZ_PAYMENT_TYPE_ID"]
)
def test_live_billz_in_production_refuses_a_missing_value(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    for name, value in LIVE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    with pytest.raises(ImproperlyConfigured, match=f"{missing} is required"):
        load_fresh_production()


@pytest.mark.parametrize(
    ("variable", "value", "match"),
    [
        ("BILLZ_SECRET_KEY", "change-me-billz-key", "placeholder"),
        ("BILLZ_SHOP_ID", "shop-1", "BILLZ_SHOP_ID must be a Billz UUID"),
        ("BILLZ_CASHBOX_ID", "42", "BILLZ_CASHBOX_ID must be a Billz UUID"),
        ("BILLZ_PAYMENT_TYPE_ID", "card", "BILLZ_PAYMENT_TYPE_ID must be a Billz UUID"),
        ("BILLZ_PLATFORM_ID", "web", "BILLZ_PLATFORM_ID must be a Billz UUID"),
        # review B16: the all-zero placeholders of .env.example are refused
        ("BILLZ_SHOP_ID", "00000000-0000-0000-0000-000000000000", "BILLZ_SHOP_ID is the .env.example"),
        ("BILLZ_CASHBOX_ID", "00000000-0000-0000-0000-000000000000", "BILLZ_CASHBOX_ID is the .env"),
        ("BILLZ_PAYMENT_TYPE_ID", "00000000-0000-0000-0000-000000000000", "BILLZ_PAYMENT_TYPE_ID is the"),
        ("BILLZ_BASE_URL", "http://api-admin.billz.ai", "https"),
    ],
)
def test_live_billz_in_production_refuses_bad_values(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    match: str,
) -> None:
    for name, good in LIVE_ENV.items():
        monkeypatch.setenv(name, good)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=match):
        load_fresh_production()


@pytest.mark.parametrize(
    ("variable", "value", "match"),
    [
        ("BILLZ_MODE", "sandbox", "BILLZ_MODE"),
        ("BILLZ_BASE_URL", "api-admin.billz.ai", "BILLZ_BASE_URL"),
        ("BILLZ_RATE_LIMIT_PER_SECOND", "0", "BILLZ_RATE_LIMIT_PER_SECOND"),
        ("BILLZ_RATE_LIMIT_PER_SECOND", "fast", "BILLZ_RATE_LIMIT_PER_SECOND"),
        ("BILLZ_READ_TIMEOUT_SECONDS", "-1", "BILLZ_READ_TIMEOUT_SECONDS"),
        ("BILLZ_CONNECT_TIMEOUT_SECONDS", "nan", "BILLZ_CONNECT_TIMEOUT_SECONDS"),
        ("BILLZ_PAGE_SIZE", "301", "BILLZ_PAGE_SIZE"),
        ("BILLZ_PAGE_SIZE", "0", "BILLZ_PAGE_SIZE"),
        ("BILLZ_FULL_SYNC_MIN_RATIO", "1.5", "BILLZ_FULL_SYNC_MIN_RATIO"),
        ("BILLZ_FULL_SYNC_MIN_RATIO", "0", "BILLZ_FULL_SYNC_MIN_RATIO"),
        # review B nit: unbounded values let one call outlive the push deadline and the renewal lock
        ("BILLZ_RATE_LIMIT_PER_SECOND", "500", "BILLZ_RATE_LIMIT_PER_SECOND must be at most 2"),
        ("BILLZ_READ_TIMEOUT_SECONDS", "900", "BILLZ_READ_TIMEOUT_SECONDS must be at most 60"),
        ("BILLZ_CONNECT_TIMEOUT_SECONDS", "31", "BILLZ_CONNECT_TIMEOUT_SECONDS must be at most 30"),
        ("BILLZ_SHIFT_RETRY_MINUTES", "0", "BILLZ_SHIFT_RETRY_MINUTES must be between 1 and 1440"),
        ("BILLZ_SHIFT_RETRY_MAX_HOURS", "169", "BILLZ_SHIFT_RETRY_MAX_HOURS must be between 1 and 168"),
        ("BILLZ_SHIFT_RETRY_MAX_HOURS", "two", "BILLZ_SHIFT_RETRY_MAX_HOURS must be an integer"),
    ],
)
def test_billz_settings_are_validated_in_every_module(
    monkeypatch: pytest.MonkeyPatch,
    load_fresh_local: Callable[[], ModuleType],
    variable: str,
    value: str,
    match: str,
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=match):
        load_fresh_local()


def test_local_settings_accept_tuned_values(
    monkeypatch: pytest.MonkeyPatch, load_fresh_local: Callable[[], ModuleType]
) -> None:
    monkeypatch.setenv("BILLZ_RATE_LIMIT_PER_SECOND", "1.5")
    monkeypatch.setenv("BILLZ_PAGE_SIZE", "200")
    monkeypatch.setenv("BILLZ_BASE_URL", "https://billz.example/")
    local = load_fresh_local()
    assert (local.BILLZ_RATE_LIMIT_PER_SECOND, local.BILLZ_PAGE_SIZE) == (1.5, 200)
    assert local.BILLZ_BASE_URL == "https://billz.example"


def test_crlf_values_are_stripped_before_they_are_validated_and_used(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review B3 (probe test_a_crlf_shop_id_passes_validation_and_zeroes_every_stock): a ``.env`` saved with
    CRLF line endings leaves ``\\r`` on every value; the adapter must use the very value that was validated,
    or no Billz shop entry ever matches and every product syncs to stock 0 and no price."""
    from glowderma.integrations.billz.live import price_of, stock_of
    from glowderma.integrations.tests.billz_support import fixture

    for name, value in LIVE_ENV.items():
        monkeypatch.setenv(name, f"{value}\r")
    monkeypatch.setenv("BILLZ_BASE_URL", "https://api-admin.billz.ai/ \r")
    production = load_fresh_production()
    assert production.BILLZ_SHOP_ID == LIVE_ENV["BILLZ_SHOP_ID"]
    assert production.BILLZ_SECRET_KEY == LIVE_ENV["BILLZ_SECRET_KEY"]
    assert production.BILLZ_CASHBOX_ID == LIVE_ENV["BILLZ_CASHBOX_ID"]
    assert production.BILLZ_PAYMENT_TYPE_ID == LIVE_ENV["BILLZ_PAYMENT_TYPE_ID"]
    assert production.BILLZ_BASE_URL == "https://api-admin.billz.ai"
    product = fixture("products_page1")["products"][0]
    assert stock_of(product, production.BILLZ_SHOP_ID) == 12
    assert price_of(product, production.BILLZ_SHOP_ID) == 185_000
