"""Malika's card (customer 5) and a generated customer's card from ``manage.py seed_demo`` at the anchor.

With only the B4a loyalty seeder (no ``commerce`` seeder yet) Malika has no purchases — her whole timeline is
replayed by the commerce seeder (03 §7.7) — so the store-only assertions check the profile parts (address,
device, groups, appointments, tier at lifetime 0) and a generated customer's store sale; the full contract
vector (balance 5 680, totals 9 940 000 / 3 640 000 / 13 580 000, next tier 6 420 000 / 0.679, 15 ledger rows)
runs once ``glowderma.commerce.seeds`` exists.
"""

from __future__ import annotations

import importlib.util
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from glowderma.accounts.seeds import MALIKA_ID
from glowderma.loyalty.seeds import store_sale_id, store_total

pytestmark = pytest.mark.django_db

COMMERCE_SEEDS = importlib.util.find_spec("glowderma.commerce.seeds") is not None
GENERATED_ID = 13


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    call_command("seed_demo", force=True, stdout=StringIO())


def card(client: APIClient, pk: int) -> dict[str, Any]:
    response = client.get(f"/api/v1/admin/customers/{pk}/")
    assert response.status_code == 200, response.data
    data: dict[str, Any] = response.json()
    return data


def test_seeded_cards_and_list(staff_client: APIClient, seeded: None) -> None:
    """One ``seed_demo`` run (≈ 13 s) for every store-only assertion."""
    check_malika_profile(staff_client)
    if not COMMERCE_SEEDS:
        check_malika_store_only_numbers(staff_client)
    check_generated_customer_store_sale(staff_client)
    check_list_first_page(staff_client)


def check_malika_profile(staff_client: APIClient) -> None:
    data = card(staff_client, MALIKA_ID)
    assert (data["id"], data["name"], data["phone"], data["status"]) == (
        MALIKA_ID,
        "Malika Karimova",
        "+998901234567",
        "active",
    )
    assert data["registered_at"] == "2025-03-12T10:20:00+05:00" and data["deleted_at"] is None
    assert data["language"] == "uz" and data["address"] == "Toshkent, Chilonzor, 9-kvartal"
    assert [group["id"] for group in data["groups"]] == [1, 2]
    assert data["groups"][0]["name"] == "Doimiy mijozlar"
    assert data["upcoming_appointments"] == 1
    assert data["device"] == {
        "platform": "ios",
        "model": "iPhone 14",
        "os": "iOS 18.1",
        "app_version": data["device"]["app_version"],
        "last_active_at": "2026-10-24T09:12:00+05:00",
        "push_enabled": True,
    }


def check_malika_store_only_numbers(staff_client: APIClient) -> None:
    data = card(staff_client, MALIKA_ID)
    assert data["totals"] == {"store": 0, "online": 0, "lifetime": 0}
    assert data["online_orders"] == {"count": 0, "last_order_id": None, "last_order_at": None}
    assert data["tier"] == {
        "policy_version": 3,
        "percent": "1.00",
        "next": {"percent": "2.00", "from": 20_000_000, "remaining": 20_000_000, "progress": 0.0},
    }
    assert (data["balance"], data["held"], data["available"]) == (0, 0, 0)
    assert data["ledger"] == [] and data["purchases"] == []


@pytest.mark.skipif(not COMMERCE_SEEDS, reason="needs the commerce seeder (B5) for Malika's orders")
def test_malika_full_contract_vector(staff_client: APIClient, seeded: None) -> None:  # pragma: no cover
    data = card(staff_client, MALIKA_ID)
    assert data["totals"] == {"store": 9_940_000, "online": 3_640_000, "lifetime": 13_580_000}
    assert data["online_orders"] == {
        "count": 12,
        "last_order_id": 10248,
        "last_order_at": "2026-10-23T14:02:00+05:00",
        "all_count": 13,  # every seeded order of Malika, any status (what orders/?customer=5 lists)
    }
    assert data["tier"] == {
        "policy_version": 3,
        "percent": "1.00",
        "next": {"percent": "2.00", "from": 20_000_000, "remaining": 6_420_000, "progress": 0.679},
    }
    assert (data["balance"], data["held"], data["available"]) == (5_680, 0, 5_680)
    assert len(data["ledger"]) == 15
    assert [row["id"] for row in data["ledger"][:3]] == [121, 120, 112]
    assert data["ledger"][2]["source_ref"] == "S-88213" and data["ledger"][2]["balance_after"] == 48_600
    adjustment = next(row for row in data["ledger"] if row["kind"] == "adjustment")
    assert adjustment["id"] == 109 and adjustment["amount"] == 2_950 and adjustment["balance_after"] == 63_150
    assert adjustment["actor"]["display_name"] == "Administrator" and adjustment["source_ref"] is None


def check_generated_customer_store_sale(staff_client: APIClient) -> None:
    amount = store_total(GENERATED_ID)
    data = card(staff_client, GENERATED_ID)
    assert amount > 0
    assert data["totals"]["store"] == amount
    store_rows = [row for row in data["ledger"] if row["source_type"] == "billz_sale"]
    assert store_rows == [
        {
            "id": store_rows[0]["id"],
            "at": "2026-09-05T12:00:00+05:00",
            "kind": "credit",
            "amount": amount // 100,
            "balance_after": store_rows[0]["balance_after"],
            "source_type": "billz_sale",
            "source_ref": store_sale_id(GENERATED_ID),
            "order_id": None,
            "base": amount,
            "percent": "1.00",
            "promo_excluded": False,
            "policy_version": 2,
            "reverses_id": None,
            "reason": None,
            "actor": None,
        }
    ]
    store_purchases = [row for row in data["purchases"] if row["source"] == "store_qr"]
    assert [(row["billz_sale_ref"], row["amount"], row["cashback"]) for row in store_purchases] == [
        (store_sale_id(GENERATED_ID), amount, amount // 100)
    ]


def check_list_first_page(staff_client: APIClient) -> None:
    page = staff_client.get("/api/v1/admin/customers/", {"ordering": "-lifetime_total"}).json()
    assert page["count"] == 432 and page["page_count"] == 22 and len(page["results"]) == 20
    totals = [row["lifetime_total"] for row in page["results"]]
    assert totals == sorted(totals, reverse=True)
    deleted = staff_client.get("/api/v1/admin/customers/", {"status": "deleted"}).json()
    assert deleted["count"] == 6 and {row["name"] for row in deleted["results"]} == {""}
    members = staff_client.get("/api/v1/admin/customers/", {"segment": 1, "page_size": 50}).json()
    assert MALIKA_ID in {row["id"] for row in members["results"]}
