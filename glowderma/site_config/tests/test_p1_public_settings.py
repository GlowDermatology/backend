"""``GET settings/public/`` (customer contract §2.4) and its cache invalidation."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.site_config import selectors, services
from glowderma.site_config.models import SiteSettings

pytestmark = pytest.mark.django_db
URL = "/api/v1/settings/public/"


def get(lang: str = "uz") -> dict:  # type: ignore[type-arg]
    response = APIClient().get(URL, HTTP_ACCEPT_LANGUAGE=lang)
    assert response.status_code == 200, response.data
    return dict(response.data)


def test_shape_and_languages() -> None:
    row = SiteSettings.objects.get(pk=1)
    row.pickup_address_uz, row.pickup_address_ru = "Toshkent, Yunusobod", "Ташкент, Юнусабад"
    row.terms_version, row.terms_uz, row.privacy_policy_uz = "2026-10-01", "Shartlar", "Siyosat"
    row.save()
    data = get("ru")
    assert set(data) == {
        "support_phone",
        "store_phone",
        "cosmetologist_phone",
        "telegram_url",
        "instagram_url",
        "website_url",
        "pickup_address",
        "privacy_policy",
        "terms",
        "booking_policy",
        "terms_version",
        "terms_updated_at",
        "reservation_minutes",
        "payment_providers",
        "online_sales_enabled",
        "fulfilment_methods",
        "min_app_version",
        "store_urls",
    }
    assert data["pickup_address"] == "Ташкент, Юнусабад"
    assert data["terms"] == "Shartlar"  # ru empty → uz
    assert data["terms_version"] == "2026-10-01"
    assert data["payment_providers"] == ["click", "payme"]
    assert data["fulfilment_methods"] == ["delivery", "pickup"]
    assert data["online_sales_enabled"] is True
    assert data["min_app_version"] == {"ios": "0.0.0", "android": "0.0.0"}
    response = APIClient().get(URL)
    assert response["Cache-Control"] == "public, max-age=60"


def test_unpublished_terms_are_null() -> None:
    row = SiteSettings.objects.get(pk=1)
    row.terms_version, row.terms_uz = "2026-10-01", ""
    row.save()
    assert get()["terms_version"] is None


def test_cache_is_invalidated_when_settings_change(
    django_capture_on_commit_callbacks: object, staff: object
) -> None:
    assert get()["reservation_minutes"] == 15
    SiteSettings.objects.filter(pk=1).update(reservation_minutes=30)
    assert get()["reservation_minutes"] == 15  # cached
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        services.update_settings({"reservation_minutes": 20}, actor=staff)
    assert get()["reservation_minutes"] == 20
    assert cache.get(selectors.PUBLIC_SETTINGS_CACHE_KEY) is not None


@override_settings(
    DEV_FEATURES_ALLOWED=False, CLICK_MODE="fake", PAYME_MODE="fake", BTS_MODE="fake", BILLZ_MODE="fake"
)
def test_fake_providers_under_the_production_settings() -> None:
    data = get()
    assert data["payment_providers"] == []
    assert data["fulfilment_methods"] == ["pickup"]
    assert data["online_sales_enabled"] is False


def test_a_snapshot_read_before_a_change_is_never_served_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request reads the row, then a settings change commits and invalidates, then the request writes its
    (old) snapshot to the cache: the next request still reads the new row, because the old snapshot carries
    the generation the invalidation retired (verification round 2, site_config)."""
    original = selectors._snapshot

    def racing() -> dict[str, Any]:
        snapshot = original()
        SiteSettings.objects.filter(pk=1).update(reservation_minutes=25)  # the change commits ...
        selectors.invalidate_public_settings()  # ... and its on-commit invalidation runs
        return snapshot

    monkeypatch.setattr(selectors, "_snapshot", racing)
    assert get()["reservation_minutes"] == 15  # this request read the row before the change
    monkeypatch.setattr(selectors, "_snapshot", original)
    assert get()["reservation_minutes"] == 25
    assert get()["reservation_minutes"] == 25  # and the fresh snapshot is cached
    SiteSettings.objects.filter(pk=1).update(reservation_minutes=40)
    assert get()["reservation_minutes"] == 25
