"""The notifications admin API in Russian and English (feat/admin-ux, DESIGN §G): the marked refusals of
``notifications.state`` / ``services`` and the admin serializers, and the campaigns / segment members
exports (headers, labels, the audience text, the file name) follow the request's ``Accept-Language``; Uzbek
stays byte for byte the source text and nothing stored changes language."""

from __future__ import annotations

import csv
import json
import re
import uuid
from datetime import datetime
from io import StringIO
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.accounts.tests.languages import overlay
from glowderma.core import uzsource
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications import services
from glowderma.notifications.models import Campaign
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CustomerSegmentFactory,
    ScheduledCampaignFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"
SEGMENTS = "/api/v1/admin/segments/"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# The texts these tests read, with their translations (the committed catalogs carry them once
# ``console_messages`` has run; the tests do not wait for that).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "Qoralamani bekor qilish shart emas: uni o‘chiring yoki o‘zgartiring.": {
        "en": "A draft does not need to be cancelled: delete or edit it instead.",
        "ru": "Черновик не нужно отменять: удалите или измените его.",
    },
    "Rejalashtirilgan xabar bu guruhga yuboriladi.": {
        "en": "A scheduled message is going to be sent to this group.",
        "ru": "Этой группе будет отправлено запланированное сообщение.",
    },
    "Sarlavhani kiriting.": {"en": "Enter a title.", "ru": "Введите заголовок."},
    "Maydonlarni tekshiring.": {"en": "Check the fields.", "ru": "Проверьте поля."},
    "Barcha · %(count)s": {"en": "All · %(count)s", "ru": "Все · %(count)s"},
    "Bitta mijoz · %(phone)s": {"en": "One customer · %(phone)s", "ru": "Один клиент · %(phone)s"},
    "bildirishnomalar": {"en": "notifications", "ru": "уведомления"},
    "guruh-azolari": {"en": "group-members", "ru": "участники-группы"},
    "Sarlavha": {"en": "Title", "ru": "Заголовок"},
    "Auditoriya": {"en": "Audience", "ru": "Аудитория"},
    "Havola": {"en": "Link", "ru": "Ссылка"},
    "Ism": {"en": "Name", "ru": "Имя"},
    "Ro‘yxatdan o‘tgan": {"en": "Registered", "ru": "Зарегистрирован"},
    "Jami xaridlar (so‘m)": {"en": "Total purchases (UZS)", "ru": "Сумма покупок (сум)"},
    "Cashback foizi": {"en": "Cashback rate", "ru": "Процент кешбэка"},
    "Balans (so‘m)": {"en": "Balance (UZS)", "ru": "Баланс (сум)"},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay(monkeypatch, TRANSLATIONS)


def _headers(language: str | None) -> dict[str, Any]:
    return {"HTTP_ACCEPT_LANGUAGE": language} if language else {}


def test_a_refused_cancel_is_rendered_in_the_request_language(
    translations: None, superuser_client: APIClient, clock: Any
) -> None:
    campaign = CampaignFactory.create()
    path = f"{URL}{campaign.pk}/cancel/"
    replies = {
        language: superuser_client.post(path, {}, format="json", **_headers(language))
        for language in (None, "en", "ru")
    }
    assert {language: r.status_code for language, r in replies.items()} == {None: 409, "en": 409, "ru": 409}
    assert replies[None]["Content-Language"] == "uz"
    assert replies[None].json()["message"] == services.MESSAGE_CANCEL_DRAFT  # Uzbek: the source text
    assert (replies["en"].json()["code"], replies["en"].json()["message"]) == (
        "invalid_transition",
        "A draft does not need to be cancelled: delete or edit it instead.",
    )
    assert replies["ru"]["Content-Language"] == "ru"
    assert replies["ru"].json()["message"] == "Черновик не нужно отменять: удалите или измените его."
    campaign.refresh_from_db()
    assert campaign.state == Campaign.State.DRAFT


def test_a_held_segment_refusal_and_a_field_error_follow_the_language(
    translations: None, staff_client: APIClient, clock: Any
) -> None:
    segment = CustomerSegmentFactory.create()
    ScheduledCampaignFactory.create(
        scheduled_at=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        dispatch_after=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        audience_type="segment",
        audience_segment=segment,
        audience_segment_name=segment.name,
    )
    refused = staff_client.delete(f"{SEGMENTS}{segment.pk}/", HTTP_ACCEPT_LANGUAGE="ru")
    assert refused.status_code == 409 and refused.json()["code"] == "conflict"
    assert refused.json()["message"] == "Этой группе будет отправлено запланированное сообщение."
    form = {"kind": "general", "title": "", "body": "Yangi mahsulotlar keldi.", "audience.type": "all"}
    invalid = staff_client.post(
        URL, form, format="multipart", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), HTTP_ACCEPT_LANGUAGE="en"
    )
    assert invalid.status_code == 400, invalid.json()
    assert [(e["field"], e["code"], e["message"]) for e in invalid.json()["errors"]] == [
        ("title", "min_length", "Enter a title.")
    ]
    uzbek = staff_client.post(URL, form, format="multipart", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
    assert [e["message"] for e in uzbek.json()["errors"]] == ["Sarlavhani kiriting."]
    stored = json.dumps(list(AuditEntry.objects.values()), default=str, ensure_ascii=False)
    assert not CYRILLIC.search(stored)


def test_the_campaigns_export_follows_the_language(
    translations: None, staff_client: APIClient, clock: Any
) -> None:
    customer = CustomerFactory.create(phone="+998901234567")
    CampaignFactory.create(
        title="Bitta",
        audience_type="customer",
        audience_customer=customer,
        audience_phone=customer.phone,
        recipient_count=1,
        created_at=datetime(2026, 10, 19, 9, tzinfo=TASHKENT),
    )
    CampaignFactory.create(
        title="Hammaga", recipient_count=4751, created_at=datetime(2026, 10, 20, 9, tzinfo=TASHKENT)
    )
    english = staff_client.get(URL, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert english["Content-Disposition"] == 'attachment; filename="notifications-20261024-1205.csv"'
    rows = list(csv.reader(StringIO(english.content.decode("utf-8-sig")), delimiter=";"))
    draft, general = uzsource.tr("Qoralama", "en"), uzsource.tr("Umumiy", "en")
    assert rows == [
        ["ID", "Date", "Type", "Title", "Audience", "Status", "Sent", "Delivered", "Link"],
        [rows[1][0], "2026-10-20 09:00", general, "Hammaga", "All · 4\u00a0751", draft, "", "", ""],
        [
            rows[2][0],
            "2026-10-19 09:00",
            general,
            "Bitta",
            "One customer · +998 90 123 45 67",
            draft,
            "",
            "",
            "",
        ],
    ]  # titles (data) are never translated
    russian = staff_client.get(URL, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="ru")
    assert "filename*=UTF-8''%D1%83%D0%B2%D0%B5%D0%B4" in russian["Content-Disposition"]
    assert "Все · 4\u00a0751" in russian.content.decode("utf-8-sig")
    uzbek = staff_client.get(URL, {"export": "csv"})
    lines = uzbek.content.decode("utf-8-sig").split("\r\n")
    assert uzbek["Content-Disposition"] == 'attachment; filename="bildirishnomalar-20261024-1205.csv"'
    assert lines[0] == '"ID";"Sana";"Turi";"Sarlavha";"Auditoriya";"Holat";"Yuborildi";"Yetkazildi";"Havola"'
    assert '"Barcha · 4\u00a0751";"Qoralama"' in lines[1] and '"Bitta mijoz · +998 90 123 45 67"' in lines[2]
    stored = json.dumps(list(AuditEntry.objects.values()), default=str, ensure_ascii=False)
    assert not CYRILLIC.search(stored) and "Audience" not in stored


def test_the_segment_members_export_follows_the_language(
    translations: None, staff_client: APIClient, clock: Any
) -> None:
    segment = CustomerSegmentFactory.create()
    response = staff_client.get(
        f"{SEGMENTS}{segment.pk}/members/", {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en"
    )
    assert response.status_code == 200 and response["Content-Language"] == "en"
    assert response["Content-Disposition"].startswith('attachment; filename="group-members-')
    header = next(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert header == [
        "ID",
        "Name",
        uzsource.tr("Telefon", "en"),
        "Registered",
        "Total purchases (UZS)",
        "Cashback rate",
        "Balance (UZS)",
        uzsource.tr("Holat", "en"),
    ]
