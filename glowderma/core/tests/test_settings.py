import base64
import importlib
import sys
from types import ModuleType
from typing import cast

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

REQUIRED = (
    "SECRET_KEY",
    "INTERNAL_API_SECRET",
    "JWT_SIGNING_KEYS",
    "JWT_ACTIVE_KID",
    "DATABASE_URL",
    "REDIS_URL",
    "ALLOWED_HOSTS",
    "ADMIN_API_ALLOWED_NETS",
)


def load_production() -> ModuleType:
    sys.modules.pop("config.settings.production", None)
    return importlib.import_module("config.settings.production")


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = dict.fromkeys(REQUIRED, "valid-secret-value-at-least-32-characters")
    values.update(
        JWT_SIGNING_KEYS='{"test":"' + base64.urlsafe_b64encode(b"x" * 32).decode() + '"}',
        JWT_ACTIVE_KID="test",
        DATABASE_URL="postgres://u:p@localhost:5433/db",
        REDIS_URL="redis://localhost:6380/0",
        ALLOWED_HOSTS="localhost",
        ADMIN_API_ALLOWED_NETS="127.0.0.0/8",
    )
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_production_settings_require_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        load_production()


def test_production_rejects_placeholder_secrets(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_KEY", "change-me-" * 8)
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        load_production()


def test_production_rejects_unknown_active_kid(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ACTIVE_KID", "unknown")
    with pytest.raises(ImproperlyConfigured, match="JWT_ACTIVE_KID"):
        load_production()


def test_production_rejects_short_jwt_key(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEYS", '{"test":"eA"}')
    with pytest.raises(ImproperlyConfigured, match="JWT_SIGNING_KEYS"):
        load_production()


def test_empty_env_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.env import env_str

    monkeypatch.setenv("EMPTY", "")
    assert env_str("EMPTY", "fallback") == "fallback"
    with pytest.raises(ImproperlyConfigured, match="EMPTY"):
        env_str("EMPTY")


@override_settings(INTERNAL_API_SECRET="")
def test_empty_secret_never_matches() -> None:
    from glowderma.core.clientip import has_internal_secret

    assert not has_internal_secret(RequestFactory().get("/"))


@override_settings(INTERNAL_API_SECRET="x" * 32)
def test_non_ascii_secret_header_is_ignored() -> None:
    from glowderma.core.clientip import has_internal_secret

    assert not has_internal_secret(RequestFactory().get("/", HTTP_X_GD_INTERNAL_SECRET="\u2603"))  # noqa: S106


def test_api_defaults_deny() -> None:
    from rest_framework.settings import api_settings

    assert api_settings.DEFAULT_AUTHENTICATION_CLASSES == []
    from glowderma.core.permissions import DenyAll

    assert cast(object, api_settings.DEFAULT_PERMISSION_CLASSES) == [DenyAll]


@override_settings(INTERNAL_API_SECRET="x" * 32)
def test_internal_secret_matches_correct_value_only() -> None:
    from glowderma.core.clientip import has_internal_secret

    factory = RequestFactory()
    assert has_internal_secret(factory.get("/", HTTP_X_GD_INTERNAL_SECRET="x" * 32))
    assert not has_internal_secret(factory.get("/", HTTP_X_GD_INTERNAL_SECRET="y" * 32))


@pytest.mark.parametrize("module_name", ["local", "production"])
@pytest.mark.parametrize("variable", ["TRUSTED_PROXIES", "ADMIN_API_ALLOWED_NETS"])
@pytest.mark.parametrize("value", ["not-a-network", "10.0.0.1/8", "10.0.0.0/8,"])
def test_invalid_cidrs_fail_settings_startup(
    production_env: None, monkeypatch: pytest.MonkeyPatch, module_name: str, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    full_name = f"config.settings.{module_name}"
    original = sys.modules.pop(full_name, None)
    try:
        with pytest.raises(ImproperlyConfigured, match=variable):
            importlib.import_module(full_name)
    finally:
        sys.modules.pop(full_name, None)
        if original is not None:
            sys.modules[full_name] = original
            setattr(sys.modules["config.settings"], module_name, original)


def test_cidrs_are_compiled_to_network_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    from ipaddress import ip_network

    from config.env import env_networks

    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8,2001:db8::/32")
    assert env_networks("TRUSTED_PROXIES") == (ip_network("10.0.0.0/8"), ip_network("2001:db8::/32"))
