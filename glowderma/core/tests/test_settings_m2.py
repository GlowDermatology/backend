"""Settings of the customer API (plan §7): validation in every module, production keys and dev switches."""

# ruff: noqa: F811 - the production fixtures are imported from the M1 settings tests and requested by name

import base64
import importlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the M1 settings tests
    LIVE_OAUTH_ENV,
    load_fresh_production,
    production_env,
)

KEY_NAMES = ("SMS_CODE_HMAC_KEY", "LOG_PII_KEY", "PAYMENT_REF_KEY", "QR_HMAC_KEY", "BTS_WEBHOOK_SECRET")
LOCAL_DEFAULTS = {
    "SMS_CODE_HMAC_KEY": "local-sms-code-hmac-key-at-least-32-characters",
    "LOG_PII_KEY": "local-log-pii-key-at-least-32-characters",
    "PAYMENT_REF_KEY": "local-payment-ref-key-at-least-32-characters",
    "QR_HMAC_KEY": "local-qr-hmac-key-at-least-32-characters",
    "BTS_WEBHOOK_SECRET": "local-bts-webhook-secret-at-least-32-characters",
    "CLICK_SECRET_KEY": "local-click-secret-key",
    "PAYME_KEY": "local-payme-key",
}


@pytest.fixture
def load_fresh_local() -> Iterator[Callable[[], ModuleType]]:
    """Re-execute ``config.settings.base`` and ``config.settings.local`` with the current environment."""
    originals = {name: sys.modules[name] for name in ("config.settings.base", "config.settings.local")}

    def load() -> ModuleType:
        for name in originals:
            sys.modules.pop(name, None)
        return importlib.import_module("config.settings.local")

    try:
        yield load
    finally:
        sys.modules.update(originals)
        sys.modules["config.settings"].base = originals["config.settings.base"]  # type: ignore[attr-defined]
        sys.modules["config.settings"].local = originals["config.settings.local"]  # type: ignore[attr-defined]


# --- every settings module ----------------------------------------------------------------------------------


def test_local_and_test_settings_allow_dev_features_with_valid_keys() -> None:
    assert settings.DEV_FEATURES_ALLOWED is True
    assert settings.ESKIZ_FAKE_CODE == "123456"
    assert len(base64.urlsafe_b64decode(settings.SECRETBOX_KEY + "=")) == 32
    assert all(len(getattr(settings, name)) >= 32 for name in KEY_NAMES)
    assert settings.CUSTOMER_ACCESS_TTL == 900 and settings.CUSTOMER_REFRESH_TTL_DAYS == 60
    assert settings.MIN_APP_VERSION_IOS == settings.MIN_APP_VERSION_ANDROID == "0.0.0"
    assert settings.CLICK_INFLIGHT_SECONDS <= settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS
    assert settings.ESKIZ_MODE == settings.INTEGRATIONS_MODE == "fake"


def test_fake_payments_require_fake_click_and_payme(
    monkeypatch: pytest.MonkeyPatch, load_fresh_local: Callable[[], ModuleType]
) -> None:
    monkeypatch.setenv("FAKE_PAYMENTS_ENABLED", "true")
    assert load_fresh_local().FAKE_PAYMENTS_ENABLED is True
    for live in ("CLICK_MODE", "PAYME_MODE"):
        monkeypatch.setenv(live, "live")
        with pytest.raises(ImproperlyConfigured, match="FAKE_PAYMENTS_ENABLED"):
            load_fresh_local()
        monkeypatch.delenv(live)


@pytest.mark.parametrize("live", ["ESKIZ_MODE", "CLICK_MODE", "PAYME_MODE", "BTS_MODE", "FCM_MODE"])
def test_scenario_header_requires_every_provider_fake(
    monkeypatch: pytest.MonkeyPatch, load_fresh_local: Callable[[], ModuleType], live: str
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO_HEADER_ENABLED", "true")
    assert load_fresh_local().FAKE_SCENARIO_HEADER_ENABLED is True
    monkeypatch.setenv(live, "live")
    with pytest.raises(ImproperlyConfigured, match="FAKE_SCENARIO_HEADER_ENABLED"):
        load_fresh_local()


@pytest.mark.parametrize(
    ("variable", "value", "match"),
    [
        ("CLICK_INFLIGHT_SECONDS", "301", "CLICK_INFLIGHT_SECONDS"),
        ("CART_MAX_LINES", "0", "CART_MAX_LINES"),
        ("CUSTOMER_OTP_TTL_SECONDS", "-5", "CUSTOMER_OTP_TTL_SECONDS"),
        ("BOOKING_CANCEL_MIN_LEAD_MINUTES", "-1", "BOOKING_CANCEL_MIN_LEAD_MINUTES"),
        ("ESKIZ_MODE", "sandbox", "ESKIZ_MODE"),
        ("MIN_APP_VERSION_IOS", "1.0", "MIN_APP_VERSION_IOS"),
        ("MIN_APP_VERSION_ANDROID", "\u0661.\u0660.\u0660", "MIN_APP_VERSION_ANDROID"),  # ASCII digits only
        ("QR_MAX_LIVE_TOKENS", "3", "QR_MAX_LIVE_TOKENS"),  # C§6.13: at most two live tokens
        ("ANDROID_SMS_APP_HASH", "short", "ANDROID_SMS_APP_HASH"),
        ("PAYME_ACCOUNT_FIELD", "Order-Id", "PAYME_ACCOUNT_FIELD"),
        ("ESKIZ_FAKE_CODE", "12ab56", "ESKIZ_FAKE_CODE"),
        ("POS_API_KEYS", "Kassa:not-a-key", "POS_API_KEYS"),
        ("THROTTLE_RATES", '{"publik": "10/min"}', "THROTTLE_RATES"),
    ],
)
def test_customer_settings_are_validated_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    load_fresh_local: Callable[[], ModuleType],
    variable: str,
    value: str,
    match: str,
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=match):
        load_fresh_local()


def test_local_parses_seeded_pos_keys_and_new_throttle_scopes(
    monkeypatch: pytest.MonkeyPatch, load_fresh_local: Callable[[], ModuleType]
) -> None:
    key = "gdpos_demo0001_local-dev-only-pos-key-000000000"
    monkeypatch.setenv("POS_API_KEYS", f"Kassa 1:{key}, Kassa 2:gdpos_demo0002_{'a' * 32}")
    monkeypatch.setenv("THROTTLE_RATES", '{"public": "10/min", "refresh_unknown": "5/min"}')
    local = load_fresh_local()
    assert local.POS_API_KEYS == [("Kassa 1", key), ("Kassa 2", f"gdpos_demo0002_{'a' * 32}")]
    assert local.THROTTLE_RATES == {"public": "10/min", "refresh_unknown": "5/min"}


# --- production -------------------------------------------------------------------------------------------


def test_production_boots_with_every_provider_fake(
    production_env: None, load_fresh_production: Callable[[], ModuleType]
) -> None:
    production = load_fresh_production()
    assert production.DEV_FEATURES_ALLOWED is False
    assert production.INTEGRATIONS_MODE == production.ESKIZ_MODE == production.CLICK_MODE == "fake"
    assert production.FAKE_PAYMENTS_ENABLED is False and production.FAKE_SCENARIO_HEADER_ENABLED is False
    assert production.ESKIZ_FAKE_CODE == "" and production.POS_API_KEYS == []
    assert set(LOCAL_DEFAULTS.values()) <= production.KNOWN_PLACEHOLDER_SECRETS


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("FAKE_PAYMENTS_ENABLED", "true"),
        ("FAKE_PAYMENTS_ENABLED", "false"),
        ("FAKE_SCENARIO_HEADER_ENABLED", "true"),
        ("ESKIZ_FAKE_CODE", "123456"),
        ("POS_API_KEYS", "Kassa 1:gdpos_demo0001_local-dev-only-pos-key-000000000"),
    ],
)
def test_production_refuses_the_dev_switches(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=f"{variable} is a local/test switch"):
        load_fresh_production()


@pytest.mark.parametrize("variable", [*KEY_NAMES, "CLICK_SECRET_KEY", "PAYME_KEY", "SECRETBOX_KEY"])
def test_production_requires_each_customer_key(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(ImproperlyConfigured, match=f"{variable} is required"):
        load_fresh_production()
    monkeypatch.setenv(variable, "Change-Me-" + "x" * 40)
    with pytest.raises(ImproperlyConfigured, match=f"{variable} is a placeholder"):
        load_fresh_production()


@pytest.mark.parametrize("variable", [*KEY_NAMES, "CLICK_SECRET_KEY", "PAYME_KEY", "SECRETBOX_KEY"])
def test_production_refuses_a_whitespace_only_customer_key(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    """A blank Payme key would otherwise boot and fail every Basic-auth check at runtime (fix pass 2)."""
    monkeypatch.setenv("INTEGRATIONS_MODE", "fake")
    monkeypatch.setenv(variable, "   ")
    with pytest.raises(ImproperlyConfigured, match=f"{variable} is required"):
        load_fresh_production()


@pytest.mark.parametrize("variable", KEY_NAMES)
def test_production_measures_hmac_keys_without_surrounding_whitespace(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, " " * 8 + "x" * 31 + " " * 8)
    with pytest.raises(ImproperlyConfigured, match=f"{variable} must be at least 32 characters"):
        load_fresh_production()


@pytest.mark.parametrize("variable", sorted(LOCAL_DEFAULTS))
def test_production_refuses_committed_key_values_even_with_fake_providers(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv("INTEGRATIONS_MODE", "fake")
    monkeypatch.setenv(variable, LOCAL_DEFAULTS[variable])
    with pytest.raises(ImproperlyConfigured, match=f"{variable} is a committed placeholder"):
        load_fresh_production()


@pytest.mark.parametrize("variable", KEY_NAMES)
def test_production_refuses_short_hmac_keys(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "x" * 31)
    with pytest.raises(ImproperlyConfigured, match=f"{variable} must be at least 32 characters"):
        load_fresh_production()


@pytest.mark.parametrize(
    "value",
    [
        base64.urlsafe_b64encode(b"x" * 31).decode(),
        base64.urlsafe_b64encode(b"x" * 33).decode(),
        "not base64 !!",
        base64.b64encode(bytes([0xFB] * 32)).decode(),  # 32 bytes in the standard alphabet ("+", "/")
        "Z2xvd2Rlcm1hLWxvY2FsLXNlY3JldGJveC1rZXktMzI",  # the committed local default
        "Z2xvd2Rlcm1hLWxvY2FsLXNlY3JldGJveC1rZXktMzI=",  # the same bytes, padded
    ],
)
def test_production_refuses_a_secretbox_key_that_is_not_32_fresh_bytes(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("SECRETBOX_KEY", value)
    with pytest.raises(ImproperlyConfigured, match="SECRETBOX_KEY"):
        load_fresh_production()


def test_production_accepts_a_padded_secretbox_key(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRETBOX_KEY", base64.urlsafe_b64encode(b"p" * 32).decode())
    assert load_fresh_production().SECRETBOX_KEY.endswith("=")


def _write_key(path: Path, key: object) -> Path:
    pem = key.private_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path.write_bytes(pem)
    return path


def test_live_apple_needs_team_key_and_a_p256_private_key(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OAUTH_MODE", "live")
    for name, value in LIVE_OAUTH_ENV.items():
        monkeypatch.setenv(name, value)
    load_fresh_production()  # no APPLE_BUNDLE_IDS: Apple is off, nothing else is required
    monkeypatch.setenv("APPLE_BUNDLE_IDS", "uz.glowderma.app")
    with pytest.raises(ImproperlyConfigured, match="APPLE_TEAM_ID is required"):
        load_fresh_production()
    monkeypatch.setenv("APPLE_TEAM_ID", "change-me-team-id")
    with pytest.raises(ImproperlyConfigured, match="APPLE_TEAM_ID is a placeholder"):
        load_fresh_production()
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APPLE_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APPLE_PRIVATE_KEY_FILE", str(tmp_path / "missing.p8"))
    with pytest.raises(ImproperlyConfigured, match="readable PEM"):
        load_fresh_production()
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("APPLE_PRIVATE_KEY_FILE", str(_write_key(tmp_path / "rsa.p8", rsa_key)))
    with pytest.raises(ImproperlyConfigured, match="EC P-256"):
        load_fresh_production()
    p384 = ec.generate_private_key(ec.SECP384R1())
    monkeypatch.setenv("APPLE_PRIVATE_KEY_FILE", str(_write_key(tmp_path / "p384.p8", p384)))
    with pytest.raises(ImproperlyConfigured, match="EC P-256"):
        load_fresh_production()
    p256 = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("APPLE_PRIVATE_KEY_FILE", str(_write_key(tmp_path / "apple.p8", p256)))
    assert load_fresh_production().APPLE_BUNDLE_IDS == ["uz.glowderma.app"]


BOOT_SCRIPT = """
import logging
import django
django.setup()
from glowderma.core import integration_modes
assert integration_modes.dev_features_allowed() is False
assert integration_modes.fake_in_production("click") and integration_modes.payable_providers() == []
assert integration_modes.fulfilment_methods() == ["pickup"] and not integration_modes.online_sales_enabled()
print("MODES", sorted(integration_modes.fake_providers()))
"""


def test_production_process_with_every_provider_fake_boots_and_warns(
    production_env: None,
) -> None:
    """Boot test in a subprocess: ``django.setup()`` under the production settings with ``INTEGRATIONS_MODE=
    fake`` succeeds and ``IntegrationsConfig.ready()`` logs the one WARNING naming every fake provider."""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.production", "INTEGRATIONS_MODE": "fake"}
    for name in ("FAKE_NOW", "ESKIZ_FAKE_CODE", "POS_API_KEYS", "FAKE_PAYMENTS_ENABLED"):
        env.pop(name, None)
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", BOOT_SCRIPT],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "integrations running in fake mode in production: oauth, billz, fcm, click, payme, bts, eskiz" in (
        result.stdout + result.stderr
    )
    assert "MODES ['billz', 'bts', 'click', 'email', 'eskiz', 'fcm', 'oauth', 'payme']" in result.stdout


def test_env_example_does_not_ship_switches_that_production_refuses_at_any_value() -> None:
    """Review nit: production refuses ``FAKE_*`` switches that are merely set (even to ``false``), so the
    example file must not offer an uncommentable ``FAKE_PAYMENTS_ENABLED=false`` line."""
    env_example = (Path(settings.BASE_DIR) / ".env.example").read_text()
    for name in ("FAKE_PAYMENTS_ENABLED", "FAKE_SCENARIO_HEADER_ENABLED"):
        assert not re.search(rf"^#?\s*{name}=", env_example, re.MULTILINE), name
