"""The payments export in English (feat/admin-ux, DESIGN §G): file name and labelled values follow the
request's ``Accept-Language``; data (names, ids, amounts) never changes and Uzbek is the source text."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from io import StringIO

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import uzsource
from glowderma.payments.tests.factories import OrderRowFactory, PaymentFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/admin/payments/"
# The texts this test reads, with their translations (the committed catalogs carry them once
# ``console_messages`` has run; the test does not wait for that).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "tolovlar": {"en": "payments", "ru": "платежи"},
    "To‘langan": {"en": "Paid", "ru": "Оплачен"},
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


def test_the_payments_export_follows_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    customer = CustomerFactory.create(name="Aziza", phone="+998935551209")
    payment = PaymentFactory.create(order=OrderRowFactory.create(customer=customer, paid=True))
    english = staff_client.get(URL, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert re.match(r'attachment; filename="payments-\d{8}-\d{4}\.csv"', english["Content-Disposition"])
    row = list(csv.reader(StringIO(english.content.decode("utf-8-sig")), delimiter=";"))[1]
    assert row[:6] == [str(payment.pk), str(payment.order_id), "Aziza", "+998935551209", "Click", "Paid"]
    uzbek = staff_client.get(URL, {"export": "csv"})
    assert 'filename="tolovlar-' in uzbek["Content-Disposition"]
    assert list(csv.reader(StringIO(uzbek.content.decode("utf-8-sig")), delimiter=";"))[1][5] == "To‘langan"
