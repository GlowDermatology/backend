"""Contract §2.6 ``POST customers/{id}/cashback-adjustments/``: the checks in contract order with their field
and top-level messages, ``insufficient_balance`` decided under the account lock (two concurrent subtracts),
idempotency, deleted customers, the audit row and 404."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer, StaffUser
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.customers.tests.helpers import make_customer, set_balance
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import LedgerEntry, LoyaltyAccount

pytestmark = pytest.mark.django_db

AMOUNT = "Summani kiriting."
REASON = "Sababni kiriting."
INSUFFICIENT = "Balans manfiy bo‘lib qoladi."


def url(pk: int) -> str:
    return f"/api/v1/admin/customers/{pk}/cashback-adjustments/"


def post(client: APIClient, pk: int, body: Any, key: str | None = None) -> Any:
    return client.post(url(pk), body, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


@pytest.fixture
def customer(staff: StaffUser, clock: Any) -> Customer:
    """A customer with balance 48 600 (posted an hour before the anchor)."""
    customer = make_customer("Malika Karimova")
    set_balance(customer, 48_600, staff, at=ANCHOR - timedelta(hours=1))
    return customer


def test_add_returns_the_entry_balance_and_available(
    staff_client: APIClient, staff: StaffUser, customer: Customer
) -> None:
    response = post(
        staff_client, customer.pk, {"direction": "add", "amount": 2_950, "reason": "  Chek S-87011  "}
    )
    assert response.status_code == 201, response.data
    entry = LedgerEntry.objects.get(account__customer=customer, amount=2_950)
    assert response.json() == {
        "entry": {
            "id": entry.pk,
            "at": "2026-10-24T12:05:00+05:00",
            "kind": "adjustment",
            "amount": 2_950,
            "balance_after": 51_550,
            "source_type": "admin",
            "source_ref": None,
            "order_id": None,
            "base": None,
            "percent": None,
            "promo_excluded": False,
            "policy_version": None,
            "reverses_id": None,
            "reason": "Chek S-87011",  # trimmed
            "actor": {"id": staff.pk, "display_name": staff.full_name or staff.email},
        },
        "balance": 51_550,
        "available": 51_550,
    }
    audit = AuditEntry.objects.get(action="cashback.adjusted")
    assert (
        audit.actor_id == staff.pk and audit.entity_type == "customer" and audit.entity_id == str(customer.pk)
    )
    assert audit.after is not None
    assert {k: audit.after[k] for k in ("direction", "amount", "reason", "balance_after")} == {
        "direction": "add",
        "amount": 2_950,
        "reason": "Chek S-87011",
        "balance_after": 51_550,
    }
    assert audit.request_id == response["X-Request-ID"]


def test_subtract_accounts_for_active_holds(staff_client: APIClient, customer: Customer) -> None:
    order = OrderFactory.create(customer=customer, cashback_used=10_000)
    loyalty_services.create_hold(customer.pk, order.pk, 10_000, ANCHOR + timedelta(minutes=15))
    ok = post(staff_client, customer.pk, {"direction": "subtract", "amount": 38_600, "reason": "Xato hisob"})
    assert ok.status_code == 201, ok.data
    assert ok.json()["balance"] == 10_000 and ok.json()["available"] == 0
    assert ok.json()["entry"]["amount"] == -38_600
    refused = post(staff_client, customer.pk, {"direction": "subtract", "amount": 1, "reason": "Xato hisob"})
    assert refused.status_code == 400
    assert refused.data["message"] == INSUFFICIENT
    assert errors(refused) == [("amount", "insufficient_balance", "Balansdan ko‘p ayirib bo‘lmaydi.")]


@pytest.mark.parametrize(
    ("body", "message", "expected"),
    [
        pytest.param(
            {"direction": "add", "amount": 0, "reason": "ab"},
            AMOUNT,
            [("amount", "min_value", AMOUNT), ("reason", "min_length", "Kamida 3 ta belgi kiriting.")],
            id="amount_then_reason",
        ),
        pytest.param(
            {"direction": "add", "amount": 10_000_001, "reason": "Sabab"},
            AMOUNT,
            [("amount", "max_value", AMOUNT)],
            id="max",
        ),
        pytest.param(
            {"direction": "add", "amount": "100", "reason": "Sabab"},
            AMOUNT,
            [("amount", "invalid", AMOUNT)],
            id="string",
        ),
        pytest.param(
            {"direction": "add", "amount": 1.5, "reason": "Sabab"},
            AMOUNT,
            [("amount", "invalid", AMOUNT)],
            id="float",
        ),
        pytest.param(
            {"direction": "add", "amount": True, "reason": "Sabab"},
            AMOUNT,
            [("amount", "invalid", AMOUNT)],
            id="bool",
        ),
        pytest.param(
            {"direction": "add", "reason": "Sabab"}, AMOUNT, [("amount", "invalid", AMOUNT)], id="missing"
        ),
        pytest.param(
            {"direction": "add", "amount": 1, "reason": "   ab   "},
            REASON,
            [("reason", "min_length", "Kamida 3 ta belgi kiriting.")],
            id="reason_trimmed",
        ),
        pytest.param(
            {"direction": "add", "amount": 1, "reason": "x" * 301},
            REASON,
            [("reason", "max_length", "300 belgidan oshmasin.")],
            id="reason_long",
        ),
        pytest.param(
            {"direction": "add", "amount": 1},
            REASON,
            [("reason", "min_length", "Kamida 3 ta belgi kiriting.")],
            id="no_reason",
        ),
        pytest.param(
            # PostgreSQL text cannot hold NUL: a 400, never a 500 (DataError) from the INSERT
            {"direction": "add", "amount": 100, "reason": "abc\u0000def"},
            REASON,
            [("reason", "invalid", "Noto‘g‘ri qiymat.")],
            id="reason_nul",
        ),
        pytest.param(
            {"direction": "add", "amount": 0, "reason": "\u0000\u0000\u0000"},
            AMOUNT,
            [("amount", "min_value", AMOUNT), ("reason", "invalid", "Noto‘g‘ri qiymat.")],
            id="amount_then_reason_nul",
        ),
        pytest.param(
            # the amount and reason checks come before the balance check
            {"direction": "subtract", "amount": 10_000_000, "reason": "x"},
            REASON,
            [("reason", "min_length", "Kamida 3 ta belgi kiriting.")],
            id="validation_before_balance",
        ),
        pytest.param(
            {"direction": "subtract", "amount": 48_601, "reason": "Sabab"},
            INSUFFICIENT,
            [("amount", "insufficient_balance", "Balansdan ko‘p ayirib bo‘lmaydi.")],
            id="insufficient",
        ),
        pytest.param(
            {"direction": "remove", "amount": 1, "reason": "Sabab"},
            "Maydonlarni tekshiring.",
            [("direction", "invalid_choice", "Ruxsat etilmagan qiymat.")],
            id="direction",
        ),
        pytest.param(
            {"amount": 1, "reason": "Sabab"},
            "Maydonlarni tekshiring.",
            [("direction", "required", "Majburiy maydon.")],
            id="direction_missing",
        ),
        pytest.param(
            [1, 2],
            "Maydonlarni tekshiring.",
            [("direction", "required", "Majburiy maydon.")],
            id="not_an_object",
        ),
    ],
)
def test_checks_in_contract_order(
    staff_client: APIClient, customer: Customer, body: Any, message: str, expected: list[tuple[str, str, str]]
) -> None:
    response = post(staff_client, customer.pk, body)
    assert response.status_code == 400, response.data
    assert response.data["code"] == "validation_error"
    assert response.data["message"] == message
    assert errors(response) == expected
    assert LoyaltyAccount.objects.get(customer=customer).balance == 48_600
    assert not AuditEntry.objects.filter(action="cashback.adjusted").exists()


def test_deleted_customer_is_allowed(staff_client: APIClient, staff: StaffUser, clock: Any) -> None:
    deleted = make_customer("", status="deleted")
    response = post(
        staff_client, deleted.pk, {"direction": "add", "amount": 5_000, "reason": "Qayta tiklash"}
    )
    assert response.status_code == 201, response.data
    assert response.json()["balance"] == 5_000 and response.json()["available"] == 5_000


def test_unknown_customer_is_404(staff_client: APIClient, clock: Any) -> None:
    response = post(staff_client, 999_999, {"direction": "nope"})
    assert response.status_code == 404 and response.data["code"] == "not_found"


def test_replay_and_key_reuse(staff_client: APIClient, customer: Customer) -> None:
    key = str(uuid.uuid4())
    body = {"direction": "subtract", "amount": 600, "reason": "Qaytarilgan chek"}
    first = post(staff_client, customer.pk, body, key)
    replay = post(staff_client, customer.pk, body, key)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json() and replay["Idempotent-Replayed"] == "true"
    assert LoyaltyAccount.objects.get(customer=customer).balance == 48_000
    assert AuditEntry.objects.filter(action="cashback.adjusted").count() == 1
    reused = post(staff_client, customer.pk, {**body, "amount": 700}, key)
    assert reused.status_code == 422 and reused.data["code"] == "idempotency_key_reused"
    missing = staff_client.post(url(customer.pk), body, format="json")
    assert missing.status_code == 400 and missing.data["code"] == "idempotency_key_required"


def test_a_failed_request_stores_nothing_and_can_be_retried(
    staff_client: APIClient, staff: StaffUser, customer: Customer
) -> None:
    key = str(uuid.uuid4())
    body = {"direction": "subtract", "amount": 50_000, "reason": "Katta ayirma"}
    assert post(staff_client, customer.pk, body, key).status_code == 400
    set_balance(customer, 2_000, staff)
    assert post(staff_client, customer.pk, body, key).status_code == 201


@pytest.mark.django_db(transaction=True)
def test_concurrent_subtracts_only_one_fits(staff: StaffUser, staff_token: str, clock: Any) -> None:
    customer = make_customer("Parallel")
    set_balance(customer, 1_000, staff, at=ANCHOR - timedelta(hours=1))
    start = threading.Event()
    results: list[int] = []
    failures: list[BaseException] = []

    def subtract() -> None:
        try:
            client = APIClient()
            client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {staff_token}",
                HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET,
            )
            assert start.wait(10)
            response = post(
                client, customer.pk, {"direction": "subtract", "amount": 700, "reason": "Parallel"}
            )
            results.append(response.status_code)
            if response.status_code == 400:
                assert response.data["message"] == INSUFFICIENT
        except BaseException as exc:  # surfaced below
            failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=subtract) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert not failures, failures
    assert sorted(results) == [201, 400]
    assert LoyaltyAccount.objects.get(customer=customer).balance == 300
    assert LedgerEntry.objects.filter(account__customer=customer, amount=-700).count() == 1
