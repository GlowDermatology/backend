"""``GET me/cashback/`` and ``GET me/cashback/ledger/`` (customer contract §2.9.1–§2.9.2, plan §4 P3)."""

from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import selectors, services
from glowderma.loyalty.models import CashbackHold, LoyaltyAccount, Purchase
from glowderma.loyalty.tests.support import OrderFactory, at, seed_policies
from glowderma.loyalty.tests.test_selectors import ADJUSTMENT_REASON, replay_malika

pytestmark = pytest.mark.django_db

CASHBACK = "/api/v1/me/cashback/"
LEDGER = "/api/v1/me/cashback/ledger/"
NOW = at(10, 26, 12, 0)


def _malika(customer: Customer) -> dict[str, Any]:
    seed_policies()
    staff = StaffUserFactory.create(full_name="Administrator")
    return {"staff": staff, **replay_malika(customer, staff)}


def test_cashback_summary_equals_the_admin_card_for_malika(
    customer: Customer, customer_client: APIClient
) -> None:
    _malika(customer)
    with pinned_clock(NOW):
        body = customer_client.get(CASHBACK).json()
    assert body == {
        "balance": 5_680,
        "held": 0,
        "available": 5_680,
        "negative": False,
        "lifetime_total": 13_580_000,
        "tier": {"from": 0, "to": 20_000_000, "percent": "1.00"},
        "next_tier": {"percent": "2.00", "from": 20_000_000, "remaining": 6_420_000, "progress": 0.679},
        "tiers": [
            {"from": 0, "to": 20_000_000, "percent": "1.00"},
            {"from": 20_000_000, "to": 30_000_000, "percent": "2.00"},
            {"from": 30_000_000, "to": 50_000_000, "percent": "3.00"},
        ],
        "earn_on_cashback_paid": True,
        "holds": [],
        "qr_ttl_seconds": 60,
    }
    # the same numbers as the admin card selectors
    card = selectors.account_summary(customer.pk)
    assert (body["balance"], body["held"], body["available"]) == (card.balance, card.held, card.available)
    assert body["lifetime_total"] == selectors.customer_totals(customer.pk).lifetime


def test_cashback_without_policy_or_purchases(customer_client: APIClient) -> None:
    body = customer_client.get(CASHBACK).json()
    assert body["tier"] is None and body["next_tier"] is None and body["tiers"] == []
    assert (body["balance"], body["available"], body["negative"], body["earn_on_cashback_paid"]) == (
        0,
        0,
        False,
        False,
    )


def test_negative_balance_flag_and_holds_with_the_effective_expiry(
    customer: Customer, customer_client: APIClient
) -> None:
    account = LoyaltyAccount.objects.create(customer=customer, balance=-700)
    assert account.balance == -700
    future = NOW + timedelta(minutes=10)
    past = NOW - timedelta(minutes=2)
    open_order = OrderFactory.create(customer=customer, reservation_expires_at=future)
    graced_order = OrderFactory.create(customer=customer, reservation_expires_at=past)
    CashbackHold.objects.create(account=account, order=open_order, amount=1_000, expires_at=future)
    CashbackHold.objects.create(account=account, order=graced_order, amount=2_000, expires_at=past)
    with override_settings(PAYMENT_IN_FLIGHT_GRACE_SECONDS=300), pinned_clock(NOW):
        body = customer_client.get(CASHBACK).json()
    assert (body["balance"], body["held"], body["available"], body["negative"]) == (-700, 3_000, -3_700, True)
    assert body["holds"] == [
        # expired but still active: shown with the in-flight grace (C§2.9.1)
        {"order_id": graced_order.pk, "amount": 2_000, "expires_at": "2026-10-26T12:03:00+05:00"},
        {"order_id": open_order.pk, "amount": 1_000, "expires_at": "2026-10-26T12:10:00+05:00"},
    ]
    assert CashbackHold.objects.get(order=graced_order).expires_at == past  # the stored value is unchanged


def test_balance_held_and_holds_come_from_one_snapshot(customer: Customer) -> None:
    """A hold captured or released between two READ COMMITTED reads cannot make ``held``/``available``
    disagree with ``holds[]``: the account and its active holds are read by one statement."""
    account = LoyaltyAccount.objects.create(customer=customer, balance=5_000)
    future = NOW + timedelta(minutes=10)
    for amount in (1_000, 1_500):
        order = OrderFactory.create(customer=customer, reservation_expires_at=future)
        CashbackHold.objects.create(account=account, order=order, amount=amount, expires_at=future)
    with CaptureQueriesContext(connection) as queries, pinned_clock(NOW):
        summary = selectors.cashback_summary(customer.pk)
    hold_reads = [query["sql"] for query in queries if "loyalty_cashbackhold" in query["sql"]]
    assert len(hold_reads) == 1 and "loyalty_loyaltyaccount" in hold_reads[0]
    assert summary.held == sum(hold.amount for hold in summary.holds) == 2_500
    assert (summary.balance, summary.available) == (5_000, 2_500)
    assert selectors.cashback_summary(CustomerFactory.create().pk).holds == []


def _full_ledger(customer: Customer) -> dict[str, Any]:
    ids = _malika(customer)
    staff = ids["staff"]
    # every remaining kind: an order cancelled after payment (debit back, credit taken), a store return and a
    # subtracting adjustment
    services.reverse_purchase(ids["10231"].purchase_id, at(10, 24, 9, 0), actor_id=staff.pk)
    store = Purchase.objects.get(billz_sale_id="S-88213")
    services.reverse_purchase(store.pk, at(10, 24, 9, 30), actor_id=staff.pk)
    services.adjust(customer.pk, "subtract", 1_000, "Qo‘lda tuzatish sababi", staff.pk, at=at(10, 24, 10, 0))
    return ids


def _by_subtitle(results: list[dict[str, Any]]) -> dict[str | None, dict[str, Any]]:
    return {row["subtitle"]: row for row in results}


def test_ledger_titles_and_subtitles_for_every_kind_in_uz(
    customer: Customer, customer_client: APIClient
) -> None:
    _full_ledger(customer)
    response = customer_client.get(LEDGER, {"page_size": 50})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 19 and body["page_size"] == 50
    rows = body["results"]
    assert [row["at"] for row in rows] == sorted((row["at"] for row in rows), reverse=True)
    newest = rows[0]
    assert newest == {
        "id": newest["id"],
        "at": "2026-10-24T10:00:00+05:00",
        "kind": "adjustment",
        "amount": -1_000,
        "balance_after": newest["balance_after"],
        "title": "Balans tuzatildi",
        "subtitle": None,
        "order_id": None,
        "source": "admin",
    }
    credit_10248 = next(r for r in rows if r["kind"] == "credit" and r["order_id"] == 10248)
    assert (credit_10248["title"], credit_10248["subtitle"], credit_10248["source"]) == (
        "Cashback hisoblandi",
        "Buyurtma №10248 · 1%",
        "online",
    )
    store_credit = next(r for r in rows if r["kind"] == "credit" and r["source"] == "store")
    assert store_credit["subtitle"] == "Do‘kondagi xarid · 1%" and store_credit["order_id"] is None
    debit = next(r for r in rows if r["kind"] == "debit" and r["order_id"] == 10248)
    assert (debit["title"], debit["subtitle"], debit["amount"]) == (
        "Cashback ishlatildi",
        "Buyurtma №10248",
        -48_600,
    )
    back = next(r for r in rows if r["kind"] == "reversal" and r["amount"] > 0)
    assert (back["title"], back["subtitle"], back["order_id"]) == (
        "Cashback qaytarildi",
        "Buyurtma №10231 bekor qilindi",
        10231,
    )
    taken = [r for r in rows if r["kind"] == "reversal" and r["amount"] < 0]
    assert {(r["title"], r["subtitle"]) for r in taken} == {
        ("Cashback bekor qilindi", "Buyurtma №10231 bekor qilindi"),
        ("Cashback bekor qilindi", "Buyurtma №10198 bekor qilindi"),
        ("Cashback bekor qilindi", "Do‘kondagi xarid qaytarildi"),
    }
    added = next(r for r in rows if r["kind"] == "adjustment" and r["amount"] > 0)
    assert (added["title"], added["subtitle"]) == ("Bonus qo‘shildi", None)
    # the staff reason never leaks
    assert ADJUSTMENT_REASON not in response.content.decode()
    assert "Qo‘lda tuzatish sababi" not in response.content.decode()
    assert "reason" not in newest


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ru",
            {
                ("credit", "online"): ("Начислен кешбэк", "Заказ №10248 · 1%"),
                ("credit", "store"): ("Начислен кешбэк", "Покупка в магазине · 1%"),
                ("debit", "online"): ("Списан кешбэк", "Заказ №10248"),
                ("reversal+", "online"): ("Кешбэк возвращён", "Заказ №10231 отменён"),
                ("reversal-", "store"): ("Кешбэк отменён", "Покупка в магазине возвращена"),
                ("adjustment+", "admin"): ("Начислен бонус", None),
                ("adjustment-", "admin"): ("Баланс скорректирован", None),
            },
        ),
        (
            "en",
            {
                ("credit", "online"): ("Cashback earned", "Order №10248 · 1%"),
                ("credit", "store"): ("Cashback earned", "In-store purchase · 1%"),
                ("debit", "online"): ("Cashback used", "Order №10248"),
                ("reversal+", "online"): ("Cashback returned", "Order №10231 cancelled"),
                ("reversal-", "store"): ("Cashback reversed", "In-store purchase returned"),
                ("adjustment+", "admin"): ("Bonus added", None),
                ("adjustment-", "admin"): ("Balance corrected", None),
            },
        ),
    ],
)
def test_ledger_texts_in_ru_and_en(
    customer: Customer, customer_client: APIClient, language: str, expected: dict[Any, Any]
) -> None:
    _full_ledger(customer)
    rows: list[dict[str, Any]] = customer_client.get(
        LEDGER, {"page_size": 50}, HTTP_ACCEPT_LANGUAGE=language
    ).json()["results"]

    def find(kind: str, source: str) -> dict[str, Any]:
        base = kind.rstrip("+-")
        sign = 1 if kind.endswith("+") else -1 if kind.endswith("-") else 0
        for row in rows:
            if row["kind"] != base or row["source"] != source:
                continue
            if sign and (row["amount"] > 0) != (sign > 0):
                continue
            if base in ("credit", "debit") and source == "online" and row["order_id"] != 10248:
                continue
            if base == "reversal" and source == "online" and row["order_id"] != 10231:
                continue
            return row
        raise AssertionError((kind, source))

    for (kind, source), (title, subtitle) in expected.items():
        row = find(kind, source)
        assert (row["title"], row["subtitle"]) == (title, subtitle), (kind, source)


def test_percent_rendering_follows_the_admin_rule() -> None:
    entry: Any = type(
        "Entry",
        (),
        {
            "pk": 1,
            "at": NOW,
            "kind": "credit",
            "amount": 57,
            "balance_after": 57,
            "source_type": "billz_sale",
            "order_id": None,
            "percent": __import__("decimal").Decimal("1.50"),
        },
    )()
    assert selectors.ledger_item_view(entry, "uz", None).subtitle == "Do‘kondagi xarid · 1,5%"
    assert selectors.ledger_item_view(entry, "en", None).subtitle == "In-store purchase · 1.5%"


def test_ledger_history_horizon_keeps_amounts_and_drops_order_links(
    customer: Customer, customer_client: APIClient
) -> None:
    _malika(customer)
    Customer.objects.filter(pk=customer.pk).update(history_from=at(10, 20))
    rows = customer_client.get(LEDGER, {"page_size": 50}).json()["results"]
    assert len(rows) == 15
    before = [r for r in rows if r["at"] < "2026-10-20T00:00:00+05:00"]
    after = [r for r in rows if r["at"] >= "2026-10-20T00:00:00+05:00"]
    assert before and after
    assert all(r["order_id"] is None and r["subtitle"] == "Avvalgi hisob" for r in before)
    assert any(r["order_id"] == 10248 for r in after)
    # amounts and the chain are kept
    assert sum(r["amount"] for r in rows) == 5_680 and rows[0]["balance_after"] == 5_680


def test_ledger_pagination_and_page_size_allow_list(customer: Customer, customer_client: APIClient) -> None:
    _malika(customer)
    first = customer_client.get(LEDGER).json()
    assert (first["count"], first["page"], first["page_size"], first["page_count"]) == (15, 1, 20, 1)
    bad = customer_client.get(LEDGER, {"page_size": 10})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "page_size"
    assert bad.json()["details"] is None


def test_ledger_shows_only_the_own_entries(customer: Customer, customer_client: APIClient) -> None:
    other = CustomerFactory.create()
    staff = StaffUserFactory.create()
    services.adjust(other.pk, "add", 5_000, "Boshqa mijoz uchun bonus", staff.pk)
    assert customer_client.get(LEDGER).json()["count"] == 0
    assert customer_client.get(CASHBACK).json()["balance"] == 0
