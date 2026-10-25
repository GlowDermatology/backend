"""Contract §2.8: ``GET`` / ``PUT notification-templates/{status}/``."""

from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.core.models import AuditEntry
from glowderma.notifications.models import StatusTemplate

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notification-templates/"


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def test_get_known_and_unknown_statuses(staff_client: APIClient) -> None:
    response = staff_client.get(URL + "handed_to_courier/")
    assert response.status_code == 200
    assert response.data == {
        "status": "handed_to_courier",
        "texts": {
            "uz": "Buyurtma №{raqam} kuryerga topshirildi. Holatni ilovada kuzating.",
            "ru": "Заказ №{raqam} передан курьеру. Следите за статусом в приложении.",
            "en": "Order №{raqam} has been handed to the courier. Track it in the app.",
        },
        "updated_at": response.data["updated_at"],
        "updated_by": None,
    }
    for status in ("awaiting_payment", "processing2", "PROCESSING"):
        missing = staff_client.get(URL + status + "/")
        assert missing.status_code == 404 and missing.data["code"] == "not_found"
    assert (
        staff_client.put(
            URL + "awaiting_payment/", {"texts": {"uz": "x" * 10, "ru": "", "en": ""}}, format="json"
        ).status_code
        == 404
    )


def test_put_updates_audits_the_diff_and_sets_the_actor(staff_client: APIClient, staff: StaffUser) -> None:
    body = {
        "texts": {
            "uz": "Buyurtma №{raqam} yetkazildi. Rahmat!",
            "ru": "Заказ №{raqam} доставлен. Спасибо!",
            "en": "",
        }
    }
    response = staff_client.put(URL + "delivered/", body, format="json")
    assert response.status_code == 200, response.data
    assert response.data["texts"] == body["texts"]
    assert response.data["updated_by"] == {"id": staff.pk, "display_name": staff.full_name}
    assert response.data["updated_at"] == "2026-10-24T12:05:00+05:00" or response.data["updated_at"].endswith(
        "+05:00"
    )
    template = StatusTemplate.objects.get(status="delivered")
    assert template.text_uz == body["texts"]["uz"] and template.updated_by == staff
    entry = AuditEntry.objects.get(action="notification_template.updated")
    assert (entry.entity_type, entry.entity_id) == ("notification_template", "delivered")
    assert entry.before == {
        "uz": "Buyurtma №{raqam} yetkazildi. Xaridingiz uchun rahmat.",
        "ru": "Заказ №{raqam} доставлен. Спасибо за покупку.",
    }
    assert entry.after == {"uz": body["texts"]["uz"], "ru": body["texts"]["ru"]}
    # meta renders the edited text and the actor
    meta = staff_client.get("/api/v1/admin/notifications/meta/").data
    delivered = next(t for t in meta["templates"] if t["status"] == "delivered")
    assert delivered["texts"]["uz"] == body["texts"]["uz"] and delivered["updated_by"]["id"] == staff.pk


@pytest.mark.parametrize(
    "texts,expected",
    [
        ({"uz": "Qisq", "ru": "", "en": ""}, [("texts.uz", "min_length", "Kamida 5 ta belgi kiriting.")]),
        ({"uz": "x" * 181, "ru": "", "en": ""}, [("texts.uz", "max_length", "180 belgidan oshmasin.")]),
        ({"uz": "Buyurtma №{raqam}", "ru": "x" * 181, "en": ""},
         [("texts.ru", "max_length", "180 belgidan oshmasin.")]),
        ({"uz": "Buyurtma {number} tayyor", "ru": "", "en": ""},
         [("texts.uz", "unknown_placeholder", "Faqat {raqam} o‘rinbosari ishlatiladi.")]),
        ({"uz": "Buyurtma №{raqam} tayyor", "ru": "Заказ {raqam", "en": "Order {id}"},
         [("texts.ru", "unknown_placeholder", "Faqat {raqam} o‘rinbosari ishlatiladi."),
          ("texts.en", "unknown_placeholder", "Faqat {raqam} o‘rinbosari ishlatiladi.")]),
        ({"uz": "Buyurtma №{raqam} tayyor"},
         [("texts.ru", "required", "Majburiy maydon."), ("texts.en", "required", "Majburiy maydon.")]),
        ({"uz": "", "ru": "", "en": ""}, [("texts.uz", "blank", "Maydonni to‘ldiring.")]),
    ],
)  # fmt: skip
def test_put_validation_table(staff_client: APIClient, texts: dict[str, str], expected: list[Any]) -> None:
    response = staff_client.put(URL + "processing/", {"texts": texts}, format="json")
    assert response.status_code == 400, response.data
    assert response.data["code"] == "validation_error" and errors(response) == expected
    assert not AuditEntry.objects.filter(action="notification_template.updated").exists()
    assert StatusTemplate.objects.get(status="processing").text_uz.startswith("To‘lov qabul qilindi")


def test_put_without_texts_and_with_repeated_placeholder(staff_client: APIClient) -> None:
    assert errors(staff_client.put(URL + "processing/", {}, format="json")) == [
        ("texts", "required", "Majburiy maydon.")
    ]
    ok = staff_client.put(
        URL + "processing/",
        {"texts": {"uz": "№{raqam}: buyurtma {raqam} tayyorlanmoqda", "ru": "", "en": ""}},
        format="json",
    )
    assert ok.status_code == 200
