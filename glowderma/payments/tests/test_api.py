"""Admin API — contract §2.12 ``GET payments/``: shape, filters (status, provider, refund_state, period,
dates, search), ordering, paging and the ``tolovlar`` export."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT
from glowderma.payments.tests.factories import OrderRowFactory, PaymentFactory, RefundFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/admin/payments/"
DAY = datetime(2026, 10, 23, 14, 2, 30, tzinfo=TASHKENT)


def _ids(response: Any) -> list[int]:
    assert response.status_code == 200, response.json()
    return [row["id"] for row in response.json()["results"]]


def test_shape_and_refunds(staff_client: APIClient) -> None:
    customer = CustomerFactory.create(name="Malika Karimova", phone="+998901234567")
    payment = PaymentFactory.create(
        order=OrderRowFactory.create(customer=customer, paid=True),
        created_at=DAY,
        confirmed_at=DAY + timedelta(minutes=2, seconds=30),
        provider_txn_id="CLK-7741920385",
    )
    refund = RefundFactory.create(payment=payment, requested_at=DAY + timedelta(days=1))
    body = staff_client.get(URL).json()
    assert (body["count"], body["page_size"]) == (1, 20)
    row = body["results"][0]
    assert row == {
        "id": payment.pk,
        "order_id": payment.order_id,
        "customer": {"id": customer.pk, "name": "Malika Karimova", "phone": "+998901234567", "email": None},
        "provider": "click",
        "status": "paid",
        "amount": payment.amount,
        "transaction_id": "CLK-7741920385",
        "created_at": "2026-10-23T14:02:30+05:00",
        "confirmed_at": "2026-10-23T14:05:00+05:00",
        "refunds": [
            {
                "id": refund.pk,
                "state": "requested",
                "provider": "click",
                "amount": refund.amount,
                "requested_at": "2026-10-24T14:02:30+05:00",
                "updated_at": row["refunds"][0]["updated_at"],
                "completed_at": None,
                "manual_reason": None,
            }
        ],
    }
    assert staff_client.get(URL, {"page_size": 10}).status_code == 400


def test_filters_search_and_ordering(staff_client: APIClient) -> None:
    # Every field the search reads (order id, transaction id, customer phone) is pinned: factory sequences
    # grow over the whole run, so a sequenced phone or ``CLK-…`` id could otherwise contain a searched term
    # (an order id, "4567") depending on which tests ran before.
    malika = CustomerFactory.create(name="Malika", phone="+998901234567")
    click = PaymentFactory.create(
        order=OrderRowFactory.create(id=777001, customer=malika, paid=True),
        created_at=DAY,
        provider_txn_id="CLK-1000001",
    )
    payme = PaymentFactory.create(
        order=OrderRowFactory.create(
            id=777002, customer=CustomerFactory.create(phone="+998935550101"), paid=True, payme=True
        ),
        provider="payme",
        created_at=DAY - timedelta(days=10),
        provider_txn_id="68fa0c3e0000000000b7c9d2",
    )
    pending = PaymentFactory.create(
        order=OrderRowFactory.create(id=777003, customer=CustomerFactory.create(phone="+998935550202")),
        pending=True,
        created_at=DAY + timedelta(hours=1),
    )
    RefundFactory.create(payment=payme, manual_required=True)
    assert _ids(staff_client.get(URL, {"status": "pending"})) == [pending.pk]
    assert _ids(staff_client.get(URL, {"provider": "payme"})) == [payme.pk]
    assert _ids(staff_client.get(URL, {"refund_state": "manual_required"})) == [payme.pk]
    assert _ids(staff_client.get(URL, {"search": str(click.order_id)})) == [click.pk]
    assert _ids(staff_client.get(URL, {"search": f"№{click.order_id}"})) == [click.pk]
    assert _ids(staff_client.get(URL, {"search": payme.provider_txn_id[-6:].upper()})) == [payme.pk]
    assert _ids(staff_client.get(URL, {"search": "4567"})) == [click.pk]
    assert _ids(staff_client.get(URL)) == [pending.pk, click.pk, payme.pk]
    assert _ids(staff_client.get(URL, {"ordering": "created_at"})) == [payme.pk, click.pk, pending.pk]
    with pinned_clock(DAY + timedelta(hours=2)):
        assert _ids(staff_client.get(URL, {"period": "today"})) == [pending.pk, click.pk]
        assert _ids(staff_client.get(URL, {"period": "week"})) == [pending.pk, click.pk]
        assert _ids(staff_client.get(URL, {"period": "month"})) == [pending.pk, click.pk, payme.pk]
    assert _ids(staff_client.get(URL, {"date_from": "2026-10-13", "date_to": "2026-10-13"})) == [payme.pk]
    bad = staff_client.get(URL, {"date_from": "2026-10-14", "date_to": "2026-10-13"})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "date_from"
    assert staff_client.get(URL, {"refund_state": "lost"}).status_code == 400


def test_export(staff_client: APIClient) -> None:
    customer = CustomerFactory.create(name="Aziza", phone="+998935551209")
    payment = PaymentFactory.create(
        order=OrderRowFactory.create(customer=customer, paid=True),
        created_at=DAY,
        confirmed_at=DAY + timedelta(minutes=3),
        provider_txn_id="CLK-1",
    )
    RefundFactory.create(payment=payment, succeeded=True)
    response = staff_client.get(URL, {"export": "csv"})
    assert response.status_code == 200 and 'filename="tolovlar-' in response["Content-Disposition"]
    header, line = response.content.decode("utf-8-sig").split("\r\n")[:2]
    assert header == (
        '"ID";"Buyurtma №";"Mijoz";"Telefon";"Usul";"Holat";"Summa (so‘m)";"Tranzaksiya";"Yaratilgan";'
        '"Tasdiqlangan";"Qaytarish"'
    )
    assert line == (
        f'"{payment.pk}";"{payment.order_id}";"Aziza";"+998935551209";"Click";"To‘langan";"{payment.amount}";'
        '"CLK-1";"2026-10-23 14:02";"2026-10-23 14:05";"Qaytarildi"'
    )
    assert staff_client.get(URL, {"export": "xlsx"}).content[:2] == b"PK"
