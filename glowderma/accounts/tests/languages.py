"""Test helper for the admin API's languages (feat/admin-ux, DESIGN §G): a translations overlay on top of the
committed catalogs, so a module's representative ru / en tests pass before and after ``console_messages`` +
``compile_locale`` (the pattern of ``core/tests/test_admin_api_languages.py``)."""

from __future__ import annotations

from functools import lru_cache

import pytest

from glowderma.core import uzsource


def overlay(monkeypatch: pytest.MonkeyPatch, entries: dict[str, dict[str, str]]) -> None:
    """Serve ``entries`` (``{uz: {en, ru}}``) on top of the committed catalogs for the rest of the test.

    Both lookups are replaced (the exact ``catalog`` and a fresh, private cache of reverse-matching
    ``templates``), so nothing of the overlay stays cached once the monkeypatch is undone."""
    real = uzsource.catalog
    added: dict[str, dict[str, str]] = {language: {} for language in uzsource.TARGET_LANGUAGES}
    for source, texts in entries.items():
        for language, text in texts.items():
            added[language][source] = text

    def catalog(language: str) -> dict[str, str]:
        if language not in uzsource.TARGET_LANGUAGES:
            return {}
        return {**real(language), **added[language]}

    monkeypatch.setattr(uzsource, "catalog", catalog)
    monkeypatch.setattr(uzsource, "templates", lru_cache(maxsize=4)(uzsource.templates.__wrapped__))
