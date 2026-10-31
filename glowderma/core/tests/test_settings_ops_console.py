"""Settings of the ops console: the environment badge label and the hashed static storage of production."""

# ruff: noqa: F811 - the production fixtures are imported from the M1 settings tests and requested by name

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the M1 settings tests
    load_fresh_production,
    production_env,
)


def test_local_and_test_keep_the_plain_static_storage() -> None:
    assert settings.OPS_ENVIRONMENT_LABEL == "LOCAL"
    assert "whitenoise" not in str(settings.STORAGES.get("staticfiles", {}).get("BACKEND", ""))


def test_production_label_and_manifest_storage(
    production_env: None, monkeypatch: pytest.MonkeyPatch, load_fresh_production: Callable[[], ModuleType]
) -> None:
    monkeypatch.delenv("OPS_ENVIRONMENT_LABEL", raising=False)
    production = load_fresh_production()
    assert production.OPS_ENVIRONMENT_LABEL == "PRODUCTION"
    assert production.STORAGES["staticfiles"]["BACKEND"] == (
        "whitenoise.storage.CompressedManifestStaticFilesStorage"
    )
    monkeypatch.setenv("ALERT_ENV", "staging")
    assert load_fresh_production().OPS_ENVIRONMENT_LABEL == "STAGING"
    monkeypatch.setenv("OPS_ENVIRONMENT_LABEL", "production")
    assert load_fresh_production().OPS_ENVIRONMENT_LABEL == "PRODUCTION"
    monkeypatch.setenv("OPS_ENVIRONMENT_LABEL", "LOCAL")
    with pytest.raises(ImproperlyConfigured, match="OPS_ENVIRONMENT_LABEL must be PRODUCTION or STAGING"):
        load_fresh_production()
