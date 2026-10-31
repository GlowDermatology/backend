"""Settings of the ops console's access: 2FA requirement, network allow-list, host, sessions, cookies."""

# ruff: noqa: F811 - the production fixtures are imported from the M1 settings tests and requested by name

from __future__ import annotations

from collections.abc import Callable
from ipaddress import ip_network
from types import ModuleType

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the M1 settings tests
    load_fresh_production,
    production_env,
)

Loader = Callable[[], ModuleType]
CONSOLE = {
    "DJANGO_ADMIN_ENABLED": "true",
    "OPS_DOMAIN": "ops.glowderma.uz",
    "OPS_ALLOWED_IPS": "91.212.1.10/32 185.74.5.0/28",
    "TRUSTED_PROXIES": "172.30.0.10/32",
}


@pytest.fixture
def console_env(production_env: None, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("OPS_ADMIN_ALLOWED_NETS", "OPS_REQUIRE_2FA", "OPS_SESSION_IDLE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in CONSOLE.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_local_and_test_defaults() -> None:
    assert settings.OPS_REQUIRE_2FA is True
    assert settings.OPS_SESSION_IDLE_SECONDS == 1800
    assert settings.OPS_SESSION_ABSOLUTE_SECONDS == 12 * 60 * 60
    assert ip_network("0.0.0.0/0") in settings.OPS_ADMIN_ALLOWED_NETS  # local: every address
    assert "django_otp.plugins.otp_static" in settings.INSTALLED_APPS
    middleware = settings.MIDDLEWARE
    assert middleware.index("django_otp.middleware.OTPMiddleware") > middleware.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )
    assert middleware.index(
        "glowderma.accounts.console.middleware.ConsoleSessionMiddleware"
    ) > middleware.index("django.contrib.messages.middleware.MessageMiddleware")


def test_an_enabled_console_boots_with_its_host_nets_and_host_only_cookies(
    console_env: pytest.MonkeyPatch, load_fresh_production: Loader
) -> None:
    production = load_fresh_production()
    assert production.OPS_ADMIN_ALLOWED_NETS == (ip_network("91.212.1.10/32"), ip_network("185.74.5.0/28"))
    assert "ops.glowderma.uz" in production.ALLOWED_HOSTS
    assert production.CSRF_TRUSTED_ORIGINS == ["https://ops.glowderma.uz"]
    assert production.SESSION_COOKIE_NAME.startswith("__Host-") and production.SESSION_COOKIE_SECURE
    assert production.CSRF_COOKIE_NAME.startswith("__Host-") and production.CSRF_COOKIE_SECURE
    assert not hasattr(production, "SESSION_COOKIE_DOMAIN")  # Django's default: host-only cookies
    assert production.SESSION_COOKIE_SAMESITE == "Strict"
    console_env.setenv("OPS_ADMIN_ALLOWED_NETS", "91.212.1.10/32,10.8.0.0/24")
    assert load_fresh_production().OPS_ADMIN_ALLOWED_NETS == (
        ip_network("91.212.1.10/32"),
        ip_network("10.8.0.0/24"),
    )


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("OPS_REQUIRE_2FA", "false", "OPS_REQUIRE_2FA must stay true"),
        ("OPS_ALLOWED_IPS", "", "OPS_ADMIN_ALLOWED_NETS is required"),
        ("OPS_ALLOWED_IPS", "0.0.0.0/0", "must not allow every address"),
        ("OPS_ALLOWED_IPS", "91.212.1.10/32 ::/0", "must not allow every address"),
        ("OPS_ALLOWED_IPS", "203.0.113.10/32", "documentation example range"),
        ("OPS_ALLOWED_IPS", "172.30.0.0/24", "must not include the TRUSTED_PROXIES"),
        ("TRUSTED_PROXIES", "", "needs TRUSTED_PROXIES"),
        ("OPS_DOMAIN", "", "OPS_DOMAIN must be the console's host name"),
        ("OPS_DOMAIN", "https://ops.glowderma.uz", "OPS_DOMAIN must be the console's host name"),
        ("OPS_SESSION_IDLE_SECONDS", "30", "OPS_SESSION_IDLE_SECONDS must be between"),
        ("OPS_SESSION_ABSOLUTE_SECONDS", "100000", "OPS_SESSION_ABSOLUTE_SECONDS must be between"),
    ],
)
def test_production_refuses_an_unsafe_console(
    console_env: pytest.MonkeyPatch, load_fresh_production: Loader, variable: str, value: str, message: str
) -> None:
    if value:
        console_env.setenv(variable, value)
    else:
        console_env.delenv(variable, raising=False)
    with pytest.raises(ImproperlyConfigured, match=message):
        load_fresh_production()


def test_a_disabled_console_needs_no_console_settings(
    production_env: None, monkeypatch: pytest.MonkeyPatch, load_fresh_production: Loader
) -> None:
    for name in ("DJANGO_ADMIN_ENABLED", "OPS_DOMAIN", "OPS_ALLOWED_IPS", "OPS_ADMIN_ALLOWED_NETS"):
        monkeypatch.delenv(name, raising=False)
    production = load_fresh_production()
    assert production.DJANGO_ADMIN_ENABLED is False
    assert production.OPS_ADMIN_ALLOWED_NETS == ()
    monkeypatch.setenv("OPS_REQUIRE_2FA", "false")
    with pytest.raises(ImproperlyConfigured, match="OPS_REQUIRE_2FA must stay true"):
        load_fresh_production()
