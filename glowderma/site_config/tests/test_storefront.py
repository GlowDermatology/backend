"""``site_config.storefront``: a settings change marks the website's settings reads stale."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.core.tests.storefront_support import revalidated
from glowderma.site_config.models import SiteSettings

pytestmark = pytest.mark.django_db


def test_a_settings_change(django_capture_on_commit_callbacks: Any) -> None:
    def change() -> None:
        row = SiteSettings.objects.get(pk=1)
        row.support_phone = "+998712000001"
        row.save(update_fields=["support_phone"])

    assert revalidated(django_capture_on_commit_callbacks, change) == {"shop:settings"}
