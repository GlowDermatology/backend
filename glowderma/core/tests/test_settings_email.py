"""The e-mail OTP settings (hybrid identity): EMAIL_MODE, the SMTP relay variables, production validation."""

# ruff: noqa: F811 - the production fixtures are imported from the M1 settings tests and requested by name

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

import pytest
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the M1 settings tests
    load_fresh_production,
    production_env,
)

SMTP_ENV = {
    "EMAIL_MODE": "smtp",
    "EMAIL_HOST": "smtp.relay.uz",
    "EMAIL_PORT": "465",
    "EMAIL_HOST_USER": "glowderma",
    "EMAIL_HOST_PASSWORD": "relay-issued-password",
    "EMAIL_USE_TLS": "false",
    "EMAIL_USE_SSL": "true",
    "EMAIL_FROM": "Glow Derma <kod@glowderma.uz>",
}


def test_production_defaults_to_the_fake_email(
    production_env: None, load_fresh_production: Callable[[], ModuleType]
) -> None:
    production = load_fresh_production()
    assert production.EMAIL_MODE == "fake" and production.SMTP_USE_TLS is True
    assert production.EMAIL_GLOBAL_HOURLY_LIMIT == 500 and production.CUSTOMER_REAUTH_FRESH_SECONDS == 600
    # Django 6.1 deprecates its EMAIL_HOST… settings: they are read into SMTP_* and never defined.
    assert not hasattr(production, "EMAIL_HOST") and not hasattr(production, "EMAIL_USE_TLS")


def test_production_accepts_a_complete_smtp_relay(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in SMTP_ENV.items():
        monkeypatch.setenv(name, value)
    production = load_fresh_production()
    assert (production.SMTP_HOST, production.SMTP_PORT, production.SMTP_USE_SSL) == (
        "smtp.relay.uz",
        465,
        True,
    )
    assert production.EMAIL_FROM == "Glow Derma <kod@glowderma.uz>"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"EMAIL_HOST": ""}, "EMAIL_HOST"),
        ({"EMAIL_FROM": "Glow Derma"}, "EMAIL_FROM"),
        ({"EMAIL_FROM": "change-me@example.uz"}, "EMAIL_FROM"),
        ({"EMAIL_USE_SSL": "false"}, "EMAIL_USE_TLS or EMAIL_USE_SSL"),
        ({"EMAIL_HOST_PASSWORD": ""}, "together"),
        ({"EMAIL_HOST_PASSWORD": "change-me-smtp-password"}, "EMAIL_HOST_PASSWORD"),
        ({"EMAIL_USE_TLS": "true"}, "mutually exclusive"),
        ({"EMAIL_MODE": "live"}, "EMAIL_MODE"),
        ({"EMAIL_PORT": "70000"}, "EMAIL_PORT"),
    ],
)
def test_production_refuses_an_incomplete_smtp_relay(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, str],
    message: str,
) -> None:
    for name, value in {**SMTP_ENV, **change}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=message):
        load_fresh_production()
