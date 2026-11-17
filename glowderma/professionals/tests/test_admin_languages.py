"""The specialists admin texts in Russian and English (feat/admin-ux, DESIGN §G): serializer and service
messages, a ``{n}`` template included, reach the reader in the request's language, and stay byte for byte
Uzbek without an ``Accept-Language``."""

from __future__ import annotations

import csv
from datetime import datetime, time, timedelta
from io import StringIO

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.languages import overlay
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.tests.conftest import ANCHOR
from glowderma.professionals.tests.factories import (
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]

BASE = "/api/v1/admin/specialists/"
UPCOMING = (
    "Mutaxassisda {n} ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Qabullar o‘zgarmaydi: avval "
    "ularni boshqa vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling, yoki baribir nofaol qiling."
)
SLUG = "Faqat lotin kichik harflari, raqamlar va chiziqcha (faqat raqam emas)."
TRANSLATIONS: dict[str, dict[str, str]] = {
    UPCOMING: {
        "en": "Upcoming appointments of the specialist (requested or confirmed): {n}. The appointments will "
        "not change: first move them to another time or specialist, or cancel them, or deactivate the "
        "specialist anyway.",
        "ru": "Предстоящих записей у специалиста (ожидающих или подтверждённых): {n}. Записи не изменятся: "
        "сначала перенесите их на другое время или к другому специалисту либо отмените, или всё равно "
        "деактивируйте специалиста.",
    },
    SLUG: {
        "en": "Only lowercase Latin letters, digits and hyphens (not digits only).",
        "ru": "Только строчные латинские буквы, цифры и дефис (не только цифры).",
    },
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay(monkeypatch, TRANSLATIONS)


def test_specialist_messages_follow_the_language(translations: None, staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    monday = ANCHOR.date() + timedelta(days=2)
    for hour in (10, 11):
        AppointmentFactory.create(
            professional=professional, start=datetime.combine(monday, time(hour), tzinfo=TASHKENT)
        )
    path = f"{BASE}{professional.pk}/"
    english = staff_client.patch(path, {"is_active": False}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 409 and english["Content-Language"] == "en"
    assert english.json()["message"].startswith(
        "Upcoming appointments of the specialist (requested or confirmed): 2."
    )
    russian = staff_client.patch(path, {"is_active": False}, HTTP_ACCEPT_LANGUAGE="ru")
    assert russian.json()["message"].startswith(
        "Предстоящих записей у специалиста (ожидающих или подтверждённых): 2."
    )
    assert staff_client.patch(path, {"is_active": False}).json()["message"] == UPCOMING.format(n=2)
    slug = staff_client.patch(path, {"slug": "Katta Harf"}, HTTP_ACCEPT_LANGUAGE="ru")
    assert slug.status_code == 400
    assert [(e["field"], e["code"], e["message"]) for e in slug.json()["errors"]] == [
        ("slug", "invalid", "Только строчные латинские буквы, цифры и дефис (не только цифры).")
    ]


def _csv(content: bytes) -> list[list[str]]:
    return list(csv.reader(StringIO(content.decode("utf-8-sig")), delimiter=";"))


def test_the_specialist_and_service_exports_follow_the_language(staff_client: APIClient) -> None:
    # the committed catalog (no overlay): the file name, headers, role / category / weekday / state labels
    nodira = ProfessionalFactory.create(name="Nodira", experience_years=8, role="dermatologist")
    service = ServiceFactory.create(
        name_uz="Konsultatsiya", short_name_uz="Konsultatsiya", category="dermatology"
    )
    ProfessionalServiceFactory.create(professional=nodira, service=service)
    for weekday in (0, 6):
        WorkingHoursFactory.create(professional=nodira, weekday=weekday)
    english = staff_client.get("/api/v1/admin/specialists/", {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert english["Content-Disposition"].startswith('attachment; filename="specialists-')
    assert _csv(english.content) == [
        [
            "ID",
            "Name",
            "Specialty",
            "Experience (years)",
            "Services",
            "Working days",
            "Upcoming appointments",
            "Status",
        ],
        [str(nodira.pk), "Nodira", "Dermatologist", "8", "Konsultatsiya", "Mon, Sun", "0", "Active"],
    ]
    russian = staff_client.get("/api/v1/admin/services/", {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="ru")
    assert russian["Content-Disposition"].startswith('attachment; filename="uslugi-')
    assert "filename*=UTF-8''%D1%83%D1%81%D0%BB%D1%83%D0%B3%D0%B8-" in russian["Content-Disposition"]
    # a service is feminine in Russian (review F2: the services export's own rendering)
    assert _csv(russian.content)[1][3] == "Дерматология" and _csv(russian.content)[1][-1] == "Активна"
    uzbek = staff_client.get("/api/v1/admin/specialists/", {"export": "csv"})
    assert uzbek["Content-Disposition"].startswith('attachment; filename="mutaxassislar-')
    assert _csv(uzbek.content)[1][2:] == ["Dermatolog", "8", "Konsultatsiya", "Du, Ya", "0", "Faol"]
