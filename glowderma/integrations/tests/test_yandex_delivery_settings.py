"""Yandex Delivery configuration (plan §3.11): the YANDEX_DELIVERY_* settings, the production refusals, the
mode predicates, the readiness probe, the startup warning and ``manage.py yandex_delivery_check``."""

# ruff: noqa: F811 - the settings fixtures are imported from the core settings tests and requested by name

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from io import StringIO
from types import ModuleType

import httpx
import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from glowderma.core import integration_modes
from glowderma.core.tests.test_settings import (  # noqa: F401 - shared fixtures
    load_fresh_production,
    production_env,
)
from glowderma.core.tests.test_settings_m2 import load_fresh_local  # noqa: F401 - shared fixture
from glowderma.integrations import apps as integrations_apps
from glowderma.integrations import httpclient
from glowderma.integrations import readiness as probe_module
from glowderma.integrations.management.commands import yandex_delivery_check
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.yandex_delivery import fake, protocol
from glowderma.integrations.yandex_delivery.live import LiveYandexDelivery

from .live_fixtures import Script, reply

TOKEN = "y0_AgAAAAB-real-looking-oauth-token"  # noqa: S105 - test value
YANDEX_ENV = (
    "YANDEX_DELIVERY_MODE",
    "YANDEX_DELIVERY_BASE_URL",
    "YANDEX_DELIVERY_TOKEN",
    "YANDEX_DELIVERY_CURRENCY",
    "YANDEX_DELIVERY_LANGUAGE",
    "YANDEX_DELIVERY_CALLBACK_URL",
    "YANDEX_DELIVERY_SKIP_CONFIRMATION_SOURCE",
    "YANDEX_DELIVERY_SKIP_CONFIRMATION_DESTINATION",
    "YANDEX_DELIVERY_PILOT_CUSTOMER_IDS",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in YANDEX_ENV:
        monkeypatch.delenv(name, raising=False)


# --- settings -------------------------------------------------------------------------------------------


def test_defaults(
    production_env: None, clean_env: None, load_fresh_production: Callable[[], ModuleType]
) -> None:
    production = load_fresh_production()
    assert production.YANDEX_DELIVERY_MODE == "fake"  # INTEGRATIONS_MODE
    assert production.YANDEX_DELIVERY_BASE_URL == "https://b2b.taxi.yandex.net"
    assert (production.YANDEX_DELIVERY_CURRENCY, production.YANDEX_DELIVERY_LANGUAGE) == ("UZS", "ru")
    assert production.YANDEX_DELIVERY_TOKEN == "" and production.YANDEX_DELIVERY_CALLBACK_URL == ""
    assert production.YANDEX_DELIVERY_SKIP_CONFIRMATION_SOURCE is False
    assert production.YANDEX_DELIVERY_SKIP_CONFIRMATION_DESTINATION is False
    assert production.YANDEX_DELIVERY_PILOT_CUSTOMER_IDS == []
    assert production.SCENARIO_PROVIDER_MODES["YANDEX_DELIVERY_MODE"] == "fake"


def test_live_starts_with_a_token_and_reads_every_value(
    production_env: None,
    clean_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "YANDEX_DELIVERY_MODE": "live",
        "YANDEX_DELIVERY_TOKEN": f" {TOKEN}\r\n",
        "YANDEX_DELIVERY_LANGUAGE": "en",
        "YANDEX_DELIVERY_CALLBACK_URL": "https://api.glowderma.uz/api/v1/webhooks/yandex-delivery/",
        "YANDEX_DELIVERY_SKIP_CONFIRMATION_DESTINATION": "true",
        "YANDEX_DELIVERY_PILOT_CUSTOMER_IDS": "12, 7",
    }.items():
        monkeypatch.setenv(name, value)
    production = load_fresh_production()
    assert production.YANDEX_DELIVERY_MODE == "live" and production.YANDEX_DELIVERY_TOKEN == TOKEN
    assert production.YANDEX_DELIVERY_LANGUAGE == "en"
    assert production.YANDEX_DELIVERY_SKIP_CONFIRMATION_DESTINATION is True
    assert production.YANDEX_DELIVERY_PILOT_CUSTOMER_IDS == [12, 7]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("YANDEX_DELIVERY_TOKEN", ""),
        ("YANDEX_DELIVERY_TOKEN", "   "),
        ("YANDEX_DELIVERY_TOKEN", "change-me"),
        ("YANDEX_DELIVERY_TOKEN", "local-payme-key"),  # a committed placeholder
        ("YANDEX_DELIVERY_BASE_URL", "http://b2b.taxi.yandex.net"),
        ("YANDEX_DELIVERY_CALLBACK_URL", "http://api.glowderma.uz/api/v1/webhooks/yandex-delivery/"),
    ],
)
def test_live_refuses_a_missing_or_placeholder_token_and_plain_http(
    production_env: None,
    clean_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("YANDEX_DELIVERY_MODE", "live")
    monkeypatch.setenv("YANDEX_DELIVERY_TOKEN", TOKEN)
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=name) as refusal:
        load_fresh_production()
    if name == "YANDEX_DELIVERY_TOKEN" and value.strip():
        assert value not in str(refusal.value)


def test_a_fake_yandex_needs_no_token_but_its_callback_must_be_https(
    production_env: None,
    clean_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_fresh_production().YANDEX_DELIVERY_MODE == "fake"
    monkeypatch.setenv("YANDEX_DELIVERY_CALLBACK_URL", "http://api.glowderma.uz/hook/")
    with pytest.raises(ImproperlyConfigured, match="YANDEX_DELIVERY_CALLBACK_URL"):
        load_fresh_production()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("YANDEX_DELIVERY_MODE", "sandbox"),
        ("YANDEX_DELIVERY_CURRENCY", "uzs"),
        ("YANDEX_DELIVERY_CURRENCY", "SUM1"),
        ("YANDEX_DELIVERY_LANGUAGE", "uz"),
        ("YANDEX_DELIVERY_CALLBACK_URL", "https://api.glowderma.uz/hook/?ref=1&"),
        ("YANDEX_DELIVERY_CALLBACK_URL", "api.glowderma.uz/hook/"),
        ("YANDEX_DELIVERY_PILOT_CUSTOMER_IDS", "12,abc"),
        ("YANDEX_DELIVERY_PILOT_CUSTOMER_IDS", "0"),
        ("YANDEX_DELIVERY_SKIP_CONFIRMATION_SOURCE", "maybe"),
    ],
)
def test_base_settings_refuse_malformed_values(
    clean_env: None,
    load_fresh_local: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=name):
        load_fresh_local()


def test_the_scenario_header_needs_a_fake_yandex(
    clean_env: None, load_fresh_local: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO_HEADER_ENABLED", "true")
    assert load_fresh_local().FAKE_SCENARIO_HEADER_ENABLED is True
    monkeypatch.setenv("YANDEX_DELIVERY_MODE", "live")
    with pytest.raises(ImproperlyConfigured, match="YANDEX_DELIVERY_MODE"):
        load_fresh_local()


# --- modes ----------------------------------------------------------------------------------------------


def test_yandex_is_a_provider_with_callbacks() -> None:
    assert integration_modes.PROVIDER_MODE_SETTINGS["yandex"] == "YANDEX_DELIVERY_MODE"
    assert "yandex" in integration_modes.CALLBACK_PROVIDERS
    with override_settings(YANDEX_DELIVERY_MODE="live"):
        assert integration_modes.provider_modes()["yandex"] == "live"
        assert "yandex" not in integration_modes.fake_providers()


@pytest.mark.parametrize(
    ("yandex", "bts", "dev", "providers"),
    [
        ("fake", "fake", True, ["yandex", "bts"]),
        ("fake", "fake", False, []),
        ("live", "fake", False, ["yandex"]),
        ("fake", "live", False, ["bts"]),
        ("live", "live", False, ["yandex", "bts"]),
    ],
)
def test_delivery_predicates(yandex: str, bts: str, dev: bool, providers: list[str]) -> None:
    with override_settings(YANDEX_DELIVERY_MODE=yandex, BTS_MODE=bts, DEV_FEATURES_ALLOWED=dev):
        assert integration_modes.delivery_providers() == providers
        assert integration_modes.yandex_delivery_available() is ("yandex" in providers)
        assert (
            integration_modes.callbacks_refused("yandex") is not integration_modes.yandex_delivery_available()
        )
        # delivery_available() / fulfilment_methods() keep meaning BTS for the released clients
        assert integration_modes.delivery_available() is ("bts" in providers)
        assert ("delivery" in integration_modes.fulfilment_methods()) is ("bts" in providers)


# --- readiness and the startup warning ------------------------------------------------------------------


def test_readiness_probes_a_live_yandex() -> None:
    assert "yandex" in probe_module.BUILDERS and "yandex" in probe_module.SYNCING_PROVIDERS
    with override_settings(YANDEX_DELIVERY_MODE="live", YANDEX_DELIVERY_TOKEN=""):
        assert probe_module.PROBE.configured()["yandex"] is False
    with override_settings(YANDEX_DELIVERY_MODE="live", YANDEX_DELIVERY_TOKEN=TOKEN):
        assert probe_module.PROBE.configured()["yandex"] is True
    with override_settings(YANDEX_DELIVERY_MODE="fake"):
        assert "yandex" not in probe_module.PROBE.configured()


@pytest.fixture
def fresh_warning(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(integrations_apps, "_yandex_alerts_warned", False)
    yield


def test_a_live_yandex_without_an_alert_webhook_warns_once(
    fresh_warning: None, caplog: pytest.LogCaptureFixture
) -> None:
    with override_settings(YANDEX_DELIVERY_MODE="fake", ALERT_WEBHOOK_URL=""):
        assert integrations_apps.warn_yandex_without_alerts() is False
    with override_settings(
        YANDEX_DELIVERY_MODE="live", ALERT_WEBHOOK_URL="https://bot.glowderma.uz/hooks/alerts"
    ):
        assert integrations_apps.warn_yandex_without_alerts() is False
    with (
        override_settings(YANDEX_DELIVERY_MODE="live", ALERT_WEBHOOK_URL=""),
        caplog.at_level(logging.WARNING, logger="glowderma.integrations"),
    ):
        django_apps.get_app_config("integrations").ready()
        assert integrations_apps.warn_yandex_without_alerts() is False  # once per process
    lines = [record.getMessage() for record in caplog.records if "ALERT_WEBHOOK_URL" in record.getMessage()]
    assert lines == ["yandex delivery is live without ALERT_WEBHOOK_URL: urgent delivery issues alert nobody"]


# --- manage.py yandex_delivery_check --------------------------------------------------------------------

WAREHOUSE_ARGS = ["--lat", "41.311151", "--lon", "69.279737", "--address", "Toshkent, Amir Temur ko'chasi, 1"]


def run_check(*args: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    try:
        call_command("yandex_delivery_check", *args, stdout=out, stderr=err)
    except SystemExit as exit_:
        return int(exit_.code or 0), out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


@pytest.mark.django_db
def test_the_check_stores_the_classes_of_the_fake() -> None:
    fake.reset()
    code, out, _ = run_check(*WAREHOUSE_ARGS)
    assert code == 0
    assert "tariffs: courier, express" in out and "check-price courier:" in out and "UZS" in out
    data = IntegrationState.objects.get(provider="yandex").data
    assert data["tariffs"] == ["courier", "express"] and data["tariffs_mode"] == "fake"
    assert data["tariffs_point"] == [69.279737, 41.311151] and data["tariffs_checked_at"]
    assert fake._claims == {}  # read-only: no claim


@pytest.mark.django_db
def test_the_check_against_the_live_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    script = (
        Script()
        .on("POST", protocol.PATH_TARIFFS, reply("yandex", "tariffs_ok"))
        .on(
            "POST",
            protocol.PATH_CHECK_PRICE,
            reply("yandex", "check_price_ok"),
            reply("yandex", "check_price_409", 409),
        )
    )
    adapter = LiveYandexDelivery(
        base_url="https://b2b.taxi.yandex.net", token=TOKEN, transport=script.transport
    )
    monkeypatch.setattr(yandex_delivery_check, "get_yandex_delivery", lambda: adapter)
    code, out, _ = run_check(
        *WAREHOUSE_ARGS, "--to-lat", "41.2854", "--to-lon", "69.2035", "--weight-kg", "2"
    )
    assert code == 1  # the express price was refused; the classes are stored anyway
    assert (
        "check-price courier: 24501 UZS (raw 24500.5000), eta 25 min, distance 7242 m, zone tashkent" in out
    )
    assert "check-price express: YandexRejected estimating.tariff.no_categories_in_zone" in out
    assert IntegrationState.objects.get(provider="yandex").data["tariffs"] == ["courier", "express"]
    destination = script.to("POST", protocol.PATH_CHECK_PRICE)[0]
    assert b"[69.2035,41.2854]" in destination.content.replace(b" ", b"")
    assert TOKEN not in out


@pytest.mark.django_db
def test_a_failed_check_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    IntegrationState.objects.filter(provider="yandex").update(data={"tariffs": ["courier"]})
    script = Script().on("POST", protocol.PATH_TARIFFS, httpx.Response(401, json={"code": "unauthorized"}))
    adapter = LiveYandexDelivery(
        base_url="https://b2b.taxi.yandex.net", token=TOKEN, transport=script.transport
    )
    monkeypatch.setattr(yandex_delivery_check, "get_yandex_delivery", lambda: adapter)
    code, _, err = run_check(*WAREHOUSE_ARGS)
    assert code == 1 and "tariffs failed: YandexUnavailable" in err
    assert IntegrationState.objects.get(provider="yandex").data == {"tariffs": ["courier"]}
    with override_settings(DEV_FEATURES_ALLOWED=False, YANDEX_DELIVERY_MODE="fake"):
        monkeypatch.undo()
        code, _, err = run_check(*WAREHOUSE_ARGS)
    assert code == 1 and "tariffs failed" in err  # a fake under the production settings invents nothing


@pytest.mark.parametrize(
    "args",
    [
        ["--lat", "91", "--lon", "69.2", "--address", "x"],
        ["--lat", "41.3", "--lon", "69.2"],
        ["--lat", "41.3", "--lon", "69.2", "--address", "x", "--weight-kg", "-1"],
    ],
)
def test_the_check_validates_its_options(args: list[str]) -> None:
    with pytest.raises(CommandError):
        call_command("yandex_delivery_check", *args, stdout=StringIO(), stderr=StringIO())


def test_the_check_refuses_an_empty_address_or_half_a_destination() -> None:
    with pytest.raises(CommandError, match="--address"):
        call_command("yandex_delivery_check", "--lat", "41.3", "--lon", "69.2", "--address", " ")
    with pytest.raises(CommandError, match="--to-lat"):
        call_command("yandex_delivery_check", *WAREHOUSE_ARGS, "--to-lat", "41.2")
