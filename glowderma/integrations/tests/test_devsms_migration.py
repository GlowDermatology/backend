"""``integrations.0007``: the ``eskiz`` ``IntegrationState`` row becomes ``devsms`` with its facts, and
back."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps
from django.utils import timezone

from glowderma.integrations.models import IntegrationState

pytestmark = pytest.mark.django_db
migration = importlib.import_module("glowderma.integrations.migrations.0007_devsms_provider")


def test_the_eskiz_row_becomes_devsms_and_back() -> None:
    IntegrationState.objects.filter(provider="devsms").delete()
    failed_at = timezone.now()
    IntegrationState.objects.create(provider="eskiz", consecutive_failures=2, last_error_at=failed_at)
    migration.eskiz_to_devsms(apps, None)
    row = IntegrationState.objects.get(provider="devsms")
    assert (row.consecutive_failures, row.last_error_at) == (2, failed_at)
    assert not IntegrationState.objects.filter(provider="eskiz").exists()
    migration.devsms_to_eskiz(apps, None)
    assert IntegrationState.objects.get(provider="eskiz").consecutive_failures == 2
    assert not IntegrationState.objects.filter(provider="devsms").exists()


def test_an_existing_devsms_row_wins_and_a_missing_one_is_created() -> None:
    IntegrationState.objects.create(provider="eskiz", consecutive_failures=5)
    migration.eskiz_to_devsms(apps, None)
    assert IntegrationState.objects.get(provider="devsms").consecutive_failures == 0
    assert not IntegrationState.objects.filter(provider="eskiz").exists()
    IntegrationState.objects.filter(provider="devsms").delete()
    migration.eskiz_to_devsms(apps, None)
    assert IntegrationState.objects.filter(provider="devsms").exists()
