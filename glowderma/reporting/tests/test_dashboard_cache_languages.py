"""A cached dashboard holds text: each console language keeps its own copy (reporting.dashboards.cached), and
the key a console action deletes still drops the dashboard for everybody."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.utils import translation
from django.utils.translation import gettext

from glowderma.reporting import dashboards


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    cache.clear()


def _builder(calls: list[str]) -> Any:
    def build() -> dict[str, Any]:
        calls.append(translation.get_language() or "")
        return {"title": gettext("Ops holati"), "number": len(calls)}

    return build


def test_each_language_gets_its_own_copy() -> None:
    calls: list[str] = []
    with translation.override("uz"):
        uzbek = dashboards.cached("probe", _builder(calls))
    with translation.override("en"):
        english = dashboards.cached("probe", _builder(calls))
        english_again = dashboards.cached("probe", _builder(calls))
    with translation.override("uz"):
        uzbek_again = dashboards.cached("probe", _builder(calls))
    assert calls == ["uz", "en"]  # built once per language, then served from the cache
    assert uzbek["title"] == uzbek_again["title"] == "Ops holati"
    assert english["title"] == english_again["title"] == "Ops status"


def test_deleting_the_dashboards_key_drops_every_language() -> None:
    calls: list[str] = []
    for code in ("uz", "ru"):
        with translation.override(code):
            dashboards.cached("probe", _builder(calls))
    cache.delete(f"{dashboards.CACHE_PREFIX}probe")  # what core.admin.forget_cached_counts does
    for code in ("uz", "ru"):
        with translation.override(code):
            dashboards.cached("probe", _builder(calls))
    assert calls == ["uz", "ru", "uz", "ru"]


def test_a_refresh_renews_the_dashboard_for_every_language() -> None:
    calls: list[str] = []
    with translation.override("ru"):
        dashboards.cached("probe", _builder(calls))
    with translation.override("uz"):
        dashboards.cached("probe", _builder(calls))
        dashboards.cached("probe", _builder(calls), refresh=True)
    with translation.override("ru"):
        russian = dashboards.cached("probe", _builder(calls))
    assert calls == ["ru", "uz", "uz", "ru"]
    assert russian["title"] == "Состояние системы"


def test_plain_data_keeps_one_shared_copy() -> None:
    calls: list[str] = []
    for code in ("uz", "en", "ru"):
        with translation.override(code):
            dashboards.cached("numbers", _builder(calls), translated=False)
    assert calls == ["uz"]
