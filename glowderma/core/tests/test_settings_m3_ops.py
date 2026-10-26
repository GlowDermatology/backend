"""Settings of the M3 operations package: the alert webhook, retention and the consistency thresholds
(docs/ops/monitoring.md, docs/ops/compliance.md)."""

# ruff: noqa: F811 - the production fixtures are imported from the M1 settings tests and requested by name

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the M1 settings tests
    load_fresh_production,
    production_env,
)
from glowderma.core.tests.test_settings_m2 import load_fresh_local  # noqa: F401

M3_OPS_VARIABLES = (
    "ALERT_WEBHOOK_URL",
    "ALERT_WEBHOOK_SECRET",
    "ALERT_DEDUPE_MINUTES",
    "ALERT_ENV",
    "WEBHOOK_INBOX_RETENTION_DAYS",
    "OUTBOX_RETENTION_DAYS",
    "RETENTION_BATCH_SIZE",
    "OUTBOX_DEFERRAL_WINDOW_HOURS",
    "CONSISTENCY_SAMPLE_SIZE",
    "CONSISTENCY_LOOKBACK_DAYS",
    "CONSISTENCY_BILLZ_SALE_MINUTES",
    "CONSISTENCY_PAYMENT_PENDING_MINUTES",
    "CONSISTENCY_REFUND_OPEN_HOURS",
    "CONSISTENCY_SHIPMENT_MINUTES",
    "CONSISTENCY_PARCEL_UNTRACKED_HOURS",
    "CONSISTENCY_LATE_CAPTURE_HOURS",
    "CONSISTENCY_OUTBOX_DEAD_HOURS",
    "CONSISTENCY_OUTBOX_OVERDUE_MINUTES",
)
SECRET = "alert-webhook-secret-at-least-32-characters"  # noqa: S105


def test_defaults() -> None:
    assert settings.ALERT_WEBHOOK_URL == "" and settings.ALERT_WEBHOOK_SECRET == ""
    assert settings.ALERT_DEDUPE_MINUTES == 30 and settings.ALERT_ENV == "local"
    assert (settings.WEBHOOK_INBOX_RETENTION_DAYS, settings.OUTBOX_RETENTION_DAYS) == (180, 30)
    assert (settings.RETENTION_BATCH_SIZE, settings.OUTBOX_DEFERRAL_WINDOW_HOURS) == (1000, 24)
    thresholds = [
        settings.CONSISTENCY_SAMPLE_SIZE,
        settings.CONSISTENCY_LOOKBACK_DAYS,
        settings.CONSISTENCY_BILLZ_SALE_MINUTES,
        settings.CONSISTENCY_PAYMENT_PENDING_MINUTES,
        settings.CONSISTENCY_REFUND_OPEN_HOURS,
        settings.CONSISTENCY_SHIPMENT_MINUTES,
        settings.CONSISTENCY_PARCEL_UNTRACKED_HOURS,
        settings.CONSISTENCY_LATE_CAPTURE_HOURS,
        settings.CONSISTENCY_OUTBOX_DEAD_HOURS,
        settings.CONSISTENCY_OUTBOX_OVERDUE_MINUTES,
    ]
    assert thresholds == [20, 7, 120, 60, 24, 60, 48, 24, 24, 60]


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("ALERT_WEBHOOK_URL", "alerts.example.uz/hook", "ALERT_WEBHOOK_URL must be an absolute http"),
        ("ALERT_WEBHOOK_URL", "ftp://alerts.example.uz/hook", "ALERT_WEBHOOK_URL must be an absolute http"),
        ("ALERT_DEDUPE_MINUTES", "0", "ALERT_DEDUPE_MINUTES must be between 1 and 1440"),
        ("WEBHOOK_INBOX_RETENTION_DAYS", "0", "WEBHOOK_INBOX_RETENTION_DAYS must be between"),
        ("RETENTION_BATCH_SIZE", "10", "RETENTION_BATCH_SIZE must be between 100 and 10000"),
        ("CONSISTENCY_SAMPLE_SIZE", "500", "CONSISTENCY_SAMPLE_SIZE must be between 1 and 200"),
        ("CONSISTENCY_OUTBOX_OVERDUE_MINUTES", "x", "CONSISTENCY_OUTBOX_OVERDUE_MINUTES must be an integer"),
    ],
)
def test_every_module_validates_the_values(
    monkeypatch: pytest.MonkeyPatch,
    load_fresh_local: Callable[[], ModuleType],
    variable: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=message):
        load_fresh_local()


def test_local_accepts_a_plain_http_alert_url(
    monkeypatch: pytest.MonkeyPatch, load_fresh_local: Callable[[], ModuleType]
) -> None:
    monkeypatch.setenv("ALERT_WEBHOOK_URL", " http://127.0.0.1:9000/alerts ")
    local = load_fresh_local()
    assert local.ALERT_WEBHOOK_URL == "http://127.0.0.1:9000/alerts" and local.ALERT_WEBHOOK_SECRET == ""


def test_production_defaults_the_alert_env_and_needs_no_alert_url(
    production_env: None, load_fresh_production: Callable[[], ModuleType]
) -> None:
    production = load_fresh_production()
    assert production.ALERT_WEBHOOK_URL == "" and production.ALERT_ENV == "production"


def test_production_requires_https_and_a_real_secret_for_alerts(
    production_env: None, monkeypatch: pytest.MonkeyPatch, load_fresh_production: Callable[[], ModuleType]
) -> None:
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://alerts.example.uz/hook")
    monkeypatch.setenv("ALERT_WEBHOOK_SECRET", SECRET)
    with pytest.raises(ImproperlyConfigured, match="ALERT_WEBHOOK_URL must be an https URL in production"):
        load_fresh_production()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://alerts.example.uz/hook")
    for secret, message in (
        ("", "ALERT_WEBHOOK_SECRET is required"),
        ("change-me-alert-webhook-secret-32-characters", "placeholder"),
        ("short-secret", "ALERT_WEBHOOK_SECRET must be at least 32 characters"),
    ):
        monkeypatch.setenv("ALERT_WEBHOOK_SECRET", secret)
        with pytest.raises(ImproperlyConfigured, match=message):
            load_fresh_production()
    monkeypatch.setenv("ALERT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ALERT_ENV", "staging")
    production = load_fresh_production()
    assert production.ALERT_WEBHOOK_URL == "https://alerts.example.uz/hook"
    assert production.ALERT_ENV == "staging"


def test_env_example_documents_every_new_variable() -> None:
    env_example = (Path(settings.BASE_DIR) / ".env.example").read_text()
    for name in M3_OPS_VARIABLES:
        assert re.search(rf"^#{name}=", env_example, re.MULTILINE), name
