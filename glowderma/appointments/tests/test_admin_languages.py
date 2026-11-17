"""The appointments admin texts in Russian and English (feat/admin-ux, DESIGN §G): the range board's
validation (``RANGE_MESSAGES``) and the booking messages reach the reader in the request's language, and stay
byte for byte Uzbek without an ``Accept-Language``."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.languages import overlay

pytestmark = pytest.mark.django_db

RANGE = "/api/v1/admin/appointments/range/"
TRANSLATIONS: dict[str, dict[str, str]] = {
    "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin.": {
        "en": "The end date must not be before the start date.",
        "ru": "Конечная дата не должна быть раньше начальной.",
    },
    "Ko‘pi bilan 42 kunlik davr.": {"en": "A period of at most 42 days.", "ru": "Период не более 42 дней."},
    "Mutaxassis topilmadi.": {"en": "Specialist not found.", "ru": "Специалист не найден."},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay(monkeypatch, TRANSLATIONS)


def messages(response: object) -> list[tuple[str, str, str]]:
    body = response.json()  # type: ignore[attr-defined]
    return [(error["field"], error["code"], error["message"]) for error in body["errors"]]


def test_range_validation_follows_the_language(translations: None, staff_client: APIClient) -> None:
    backwards = {"from": "2026-10-27", "to": "2026-10-26"}
    russian = staff_client.get(RANGE, backwards, HTTP_ACCEPT_LANGUAGE="ru")
    assert russian.status_code == 400 and russian["Content-Language"] == "ru"
    assert messages(russian) == [("to", "empty_range", "Конечная дата не должна быть раньше начальной.")]
    too_long = staff_client.get(RANGE, {"from": "2026-10-01", "to": "2026-11-12"}, HTTP_ACCEPT_LANGUAGE="en")
    assert messages(too_long) == [("to", "out_of_range", "A period of at most 42 days.")]
    unknown = {"from": "2026-10-26", "to": "2026-10-26", "specialist_id": "999999"}
    assert messages(staff_client.get(RANGE, unknown, HTTP_ACCEPT_LANGUAGE="en")) == [
        ("specialist_id", "does_not_exist", "Specialist not found.")
    ]
    uzbek = staff_client.get(RANGE, backwards)
    assert messages(uzbek) == [("to", "empty_range", "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin.")]
