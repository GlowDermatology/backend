"""The orders admin API in Russian and English (feat/admin-ux, DESIGN §G): the marked refusals of
``commerce.services`` and the admin views, a message template whose value is itself a marked text, and the
orders export's labels follow the request's ``Accept-Language``; Uzbek stays byte for byte the source text
and nothing stored changes language."""

from __future__ import annotations

import csv
import json
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.commerce import services
from glowderma.commerce.models import OrderEvent
from glowderma.commerce.tests.support import AT, place, ship
from glowderma.core import uzsource
from glowderma.core.models import AuditEntry
from glowderma.core.seeding import pinned_clock

pytestmark = pytest.mark.django_db
URL = "/api/v1/admin/orders/"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# The texts these tests read, with their translations (the committed catalogs carry them once
# ``console_messages`` has run; the tests do not wait for that).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "Bu buyurtmani bekor qilib bo‘lmaydi.": {
        "en": "This order cannot be cancelled.",
        "ru": "Этот заказ нельзя отменить.",
    },
    "BTS buyurtmani bekor qilmadi: {reason}.": {
        "en": "BTS did not cancel the order: {reason}.",
        "ru": "BTS не отменил заказ: {reason}.",
    },
    "posilka kuryerda": {"en": "the parcel is with the courier", "ru": "посылка у курьера"},
    "Olib ketiladigan buyurtma uchun yetkazish muammosi ochilmaydi.": {
        "en": "A delivery issue cannot be opened for a pickup order.",
        "ru": "Для заказа с самовывозом нельзя открыть проблему доставки.",
    },
    "To‘langan": {"en": "Paid", "ru": "Оплачен"},
    "Yetkazib berish": {"en": "Delivery", "ru": "Доставка"},
    "Jarayonda": {"en": "Processing", "ru": "В обработке"},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real = uzsource.catalog

    def catalog(language: str) -> dict[str, str]:
        if language not in uzsource.TARGET_LANGUAGES:
            return {}
        return {**real(language), **{uz: texts[language] for uz, texts in TRANSLATIONS.items()}}

    monkeypatch.setattr(uzsource, "catalog", catalog)
    uzsource.templates.cache_clear()
    yield
    uzsource.templates.cache_clear()


def _post(client: APIClient, path: str, body: Any, language: str | None = None) -> Any:
    headers: dict[str, Any] = {"HTTP_ACCEPT_LANGUAGE": language} if language else {}
    return client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), **headers)


def test_a_refused_cancel_is_rendered_in_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    order = place()
    path = f"{URL}{order.pk}/cancel/"
    assert _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"}).status_code == 200
    english = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"}, "en")
    assert english.status_code == 409 and english["Content-Language"] == "en"
    assert (english.json()["code"], english.json()["message"]) == (
        "not_cancellable",
        "This order cannot be cancelled.",
    )
    uzbek = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"})
    assert uzbek.json()["message"] == "Bu buyurtmani bekor qilib bo‘lmaydi."


def test_a_bts_refusal_renders_its_template_and_its_marked_reason(
    translations: None, staff_client: APIClient
) -> None:
    refused = ship(place())
    with pinned_clock(AT + timedelta(hours=2)):
        services.apply_bts_status(refused.pk, "200", "webhook")
    path = f"{URL}{refused.pk}/cancel/"
    russian = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"}, "ru")
    assert russian.status_code == 409
    assert russian.json()["message"] == "BTS не отменил заказ: посылка у курьера."
    uzbek = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"})
    assert uzbek.json()["message"] == "BTS buyurtmani bekor qilmadi: posilka kuryerda."
    # the refusal's audit rows and events stay Uzbek
    stored = json.dumps(
        [*AuditEntry.objects.values(), *OrderEvent.objects.values()], default=str, ensure_ascii=False
    )
    assert not CYRILLIC.search(stored)


def test_a_view_conflict_is_rendered_in_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    order = place(fulfilment="pickup")
    response = _post(
        staff_client, f"{URL}{order.pk}/issues/", {"kind": "incident", "note": "Mijoz kelmadi"}, "en"
    )
    assert response.status_code == 409
    assert response.json()["message"] == "A delivery issue cannot be opened for a pickup order."


def test_the_orders_export_labels_its_values_in_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    order = ship(place())
    english = staff_client.get(URL, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert re.match(r'attachment; filename="orders-\d{8}-\d{4}\.csv"', english["Content-Disposition"])
    rows = list(csv.reader(StringIO(english.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[1][0] == str(order.pk)
    assert rows[1][5:9] == ["Paid", "Click", "Delivery", "Processing"]
    uzbek = list(
        csv.reader(
            StringIO(staff_client.get(URL, {"export": "csv"}).content.decode("utf-8-sig")), delimiter=";"
        )
    )
    assert uzbek[1][5:9] == ["To‘langan", "Click", "Yetkazib berish", "Jarayonda"]
