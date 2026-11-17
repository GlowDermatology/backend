"""The customers and cashback admin API in Russian and English (feat/admin-ux, DESIGN §G): the marked
messages of ``loyalty.services`` (cashback adjustments, policy versions) and the customers export follow
the request's ``Accept-Language``; Uzbek stays byte for byte the source text and the ledger and audit rows
stay Uzbek."""

from __future__ import annotations

import csv
import json
import re
import uuid
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.tests.languages import overlay
from glowderma.core import uzsource
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.customers.tests.helpers import make_customer, set_balance
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import LedgerEntry

pytestmark = pytest.mark.django_db

CUSTOMERS = "/api/v1/admin/customers/"
VERSIONS = "/api/v1/admin/cashback/policy-versions/"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
# The texts these tests read, with their translations (the committed catalogs carry them once
# ``console_messages`` has run; the tests do not wait for that).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "Summani kiriting.": {"en": "Enter an amount.", "ru": "Укажите сумму."},
    "Balans manfiy bo‘lib qoladi.": {
        "en": "The balance would become negative.",
        "ru": "Баланс станет отрицательным.",
    },
    "Balansdan ko‘p ayirib bo‘lmaydi.": {
        "en": "You cannot deduct more than the balance.",
        "ru": "Нельзя списать больше, чем есть на балансе.",
    },
    "Kamida bitta daraja kerak.": {
        "en": "At least one tier is required.",
        "ru": "Нужен хотя бы один уровень.",
    },
    "Foiz 0 dan katta va 100 dan oshmasligi kerak.": {
        "en": "The percentage must be greater than 0 and no more than 100.",
        "ru": "Процент должен быть больше 0 и не больше 100.",
    },
    "mijozlar": {"en": "customers", "ru": "клиенты"},
    "Ism": {"en": "Name", "ru": "Имя"},
    "Ro‘yxatdan o‘tgan": {"en": "Registered", "ru": "Зарегистрирован"},
    "Jami xaridlar (so‘m)": {"en": "Total purchases (UZS)", "ru": "Сумма покупок (сум)"},
    "Cashback foizi": {"en": "Cashback rate", "ru": "Процент кешбэка"},
    "Balans (so‘m)": {"en": "Balance (UZS)", "ru": "Баланс (сум)"},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay(monkeypatch, TRANSLATIONS)


@pytest.fixture
def customer(staff: StaffUser, clock: Any) -> Customer:
    customer = make_customer("Malika Karimova", phone="+998901234567")
    set_balance(customer, 48_600, staff, at=ANCHOR - timedelta(hours=1))
    return customer


def _post(client: APIClient, path: str, body: Any, language: str | None = None) -> Any:
    headers: dict[str, Any] = {"HTTP_ACCEPT_LANGUAGE": language} if language else {}
    return client.post(path, body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), **headers)


def _errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.json()["errors"]]


def test_cashback_adjustment_refusals_follow_the_language(
    translations: None, staff_client: APIClient, customer: Customer
) -> None:
    path = f"{CUSTOMERS}{customer.pk}/cashback-adjustments/"
    english = _post(staff_client, path, {"direction": "add", "amount": 0, "reason": "Chek S-87011"}, "en")
    assert english.status_code == 400 and english["Content-Language"] == "en"
    assert english.json()["message"] == "Enter an amount."
    assert _errors(english) == [("amount", "min_value", "Enter an amount.")]
    body = {"direction": "subtract", "amount": 50_000, "reason": "Qaytarilgan xarid"}
    russian = _post(staff_client, path, body, "ru")
    assert russian.status_code == 400 and russian.json()["message"] == "Баланс станет отрицательным."
    assert _errors(russian) == [
        ("amount", "insufficient_balance", "Нельзя списать больше, чем есть на балансе.")
    ]
    uzbek = _post(staff_client, path, body)
    assert uzbek.json()["message"] == loyalty_services.MESSAGE_INSUFFICIENT == "Balans manfiy bo‘lib qoladi."
    assert _errors(uzbek) == [("amount", "insufficient_balance", loyalty_services.MESSAGE_INSUFFICIENT_FIELD)]
    stored = json.dumps(
        [*LedgerEntry.objects.values(), *AuditEntry.objects.values()], default=str, ensure_ascii=False
    )
    assert not CYRILLIC.search(stored)


def test_policy_version_refusals_follow_the_language(
    translations: None, staff_client: APIClient, clock: Any
) -> None:
    def body(*tiers: dict[str, Any]) -> dict[str, Any]:
        return {
            "effective_from": "2026-11-01T00:00:00+05:00",
            "basis": "before_purchase",
            "earn_on_cashback_paid": False,
            "tiers": list(tiers),
        }

    empty = _post(staff_client, VERSIONS, body(), "ru")
    assert empty.status_code == 400 and empty.json()["message"] == "Нужен хотя бы один уровень."
    assert _errors(empty) == [("tiers", "min_items", "Нужен хотя бы один уровень.")]
    too_high = _post(staff_client, VERSIONS, body({"from": 0, "to": None, "percent": "101"}), "en")
    assert too_high.status_code == 400
    assert too_high.json()["message"] == "The percentage must be greater than 0 and no more than 100."
    uzbek = _post(staff_client, VERSIONS, body())
    assert uzbek.json()["message"] == loyalty_services.MESSAGE_MIN_ITEMS


def test_the_customers_export_follows_the_language(
    translations: None, staff_client: APIClient, customer: Customer
) -> None:
    english = staff_client.get(CUSTOMERS, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert english["Content-Disposition"] == 'attachment; filename="customers-20261024-1205.csv"'
    rows = list(csv.reader(StringIO(english.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == [
        "ID",
        "Name",
        uzsource.tr("Telefon", "en"),
        "Registered",
        "Total purchases (UZS)",
        "Cashback rate",
        "Balance (UZS)",
        uzsource.tr("Holat", "en"),
    ]
    assert rows[1][1] == "Malika Karimova" and rows[1][-1] == uzsource.tr("Faol", "en") != "Faol"
    russian = staff_client.get(CUSTOMERS, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="ru")
    disposition = russian["Content-Disposition"]
    assert (
        disposition.startswith('attachment; filename="klienty-') and "filename*=UTF-8''%D0%BA" in disposition
    )
    uzbek = staff_client.get(CUSTOMERS, {"export": "csv"})
    assert uzbek["Content-Disposition"] == 'attachment; filename="mijozlar-20261024-1205.csv"'
    assert uzbek.content.decode("utf-8-sig").split("\r\n")[1].endswith('"Faol"')
    stored = json.dumps(list(AuditEntry.objects.values()), default=str, ensure_ascii=False)
    assert not CYRILLIC.search(stored)
