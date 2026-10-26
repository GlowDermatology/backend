"""Fix pass 1: ``settings/public/`` falls back to the database while the cache is down."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache

from glowderma.site_config import selectors

pytestmark = pytest.mark.django_db


class CacheDown:
    def __getattr__(self, name: str) -> Any:
        return getattr(cache, name)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")

    def get_many(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")

    def add(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")

    def set(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")


def test_public_settings_survive_a_cache_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = selectors.public_settings("uz")
    monkeypatch.setattr(selectors, "cache", CacheDown())
    assert selectors.public_settings("uz") == expected


def test_public_settings_survive_a_failed_cache_write(monkeypatch: pytest.MonkeyPatch) -> None:
    class WriteFails(CacheDown):
        def get(self, *args: Any, **kwargs: Any) -> Any:
            return None

        def get_many(self, *args: Any, **kwargs: Any) -> Any:
            return {}

    expected = selectors.public_settings("ru")
    monkeypatch.setattr(selectors, "cache", WriteFails())
    assert selectors.public_settings("ru") == expected


def test_public_settings_survive_a_failed_generation_write(monkeypatch: pytest.MonkeyPatch) -> None:
    class GenerationWriteFails(CacheDown):
        def get_many(self, *args: Any, **kwargs: Any) -> Any:
            return {}

    expected = selectors.public_settings("en")
    cache.clear()
    monkeypatch.setattr(selectors, "cache", GenerationWriteFails())
    assert selectors.public_settings("en") == expected
