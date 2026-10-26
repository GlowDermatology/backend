"""Review B15: the URLconf is imported once under the test settings, never first inside a test's overrides."""

import sys

import pytest
from django.core import checks
from django.test import override_settings
from django.urls import reverse


@pytest.mark.django_db
def test_url_checks_under_production_overrides_keep_the_admin_routes() -> None:
    """``config.urls`` builds its routes at import time; the root conftest imports it before any test, so a
    deploy check run under ``DJANGO_ADMIN_ENABLED=False`` (the production value) no longer caches a URLconf
    without the Django admin for every later test ("'admin' is not a registered namespace")."""
    assert "config.urls" in sys.modules
    with override_settings(DJANGO_ADMIN_ENABLED=False):
        checks.run_checks(tags=[checks.Tags.urls])
    assert reverse("admin:index") == "/django-admin/"
