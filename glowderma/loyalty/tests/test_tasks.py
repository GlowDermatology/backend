"""loyalty.reconcile_balances (design 02 §1.9.3): clean service-built chains pass; a raw-SQL-tampered balance,
a broken chain, a broken purchase link and a broken captured hold are audited, counted and never fixed."""

import pytest
from django.db import connection

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core.models import AuditEntry
from glowderma.loyalty import services, tasks
from glowderma.loyalty.models import CashbackHold, LedgerEntry, LoyaltyAccount, Purchase
from glowderma.loyalty.tests.factories import LedgerEntryFactory, LoyaltyAccountFactory
from glowderma.loyalty.tests.support import at, paid_order, seed_policies

pytestmark = pytest.mark.django_db


def findings() -> list[dict[str, object]]:
    rows = AuditEntry.objects.filter(action="loyalty.reconciliation_mismatch").order_by("id")
    return [{"entity": (row.entity_type, row.entity_id), **(row.after or {})} for row in rows]


def build_chains() -> tuple[LoyaltyAccount, services.PurchaseResult, int]:
    seed_policies()
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    services.record_store_purchase(
        customer.pk, "S-1", "S-1", 3_000_000, at(10, 1, 9), at=at(10, 1, 9)
    )  # +30 000
    order = paid_order(customer, created_at=at(10, 18, 15, 20), items_subtotal=554_000, promo_subtotal=240_000, cashback_used=20_000)  # fmt: skip  # noqa: E501
    services.create_hold(customer.pk, order.pk, 20_000, order.reservation_expires_at)
    services.capture_hold(order.pk, at=order.paid_at)
    purchase = services.record_online_purchase(customer.pk, order.pk, 554_000, 240_000, 20_000, order.paid_at)
    services.adjust(customer.pk, "add", 2_950, "qo‘lda bog‘landi", staff.pk, at=at(10, 19))
    other = CustomerFactory.create()
    services.adjust(other.pk, "add", 1_000, "sovg‘a", staff.pk, at=at(10, 19, 0, 1))
    LoyaltyAccountFactory.create()  # an account without entries is consistent too
    return LoyaltyAccount.objects.get(customer=customer), purchase, order.pk


def test_clean_chains_produce_no_mismatch_and_a_summary() -> None:
    account, _purchase, _ = build_chains()
    assert account.balance == 30_000 - 20_000 + 3_140 + 2_950
    summary = tasks.reconcile_balances.apply().get()  # the Celery task itself, run in-process
    assert summary == {
        "accounts": 3,
        "mismatches": 0,
        "negative_balances": 0,
        "checks": {
            "ledger_sum": 0,
            "last_balance_after": 0,
            "chain": 0,
            "purchase_links": 0,
            "captured_holds": 0,
        },
    }
    assert findings() == []
    completed = AuditEntry.objects.get(action="loyalty.reconciliation_completed")
    assert (completed.actor_type, completed.entity_type, completed.entity_id, completed.after) == ("system", "loyalty", "ledger", summary)  # fmt: skip  # noqa: E501


def test_raw_sql_tampered_balance_is_detected_and_not_fixed() -> None:
    account, _purchase, _ = build_chains()
    with connection.cursor() as cursor:  # loyalty_loyaltyaccount is not append-only: a raw write goes through
        cursor.execute(
            "UPDATE loyalty_loyaltyaccount SET balance = balance + 1000 WHERE id = %s", [account.pk]
        )
    summary = tasks.reconcile_balances_now()
    assert summary["mismatches"] == 2 and summary["checks"]["ledger_sum"] == 1 and summary["checks"]["last_balance_after"] == 1  # fmt: skip  # noqa: E501
    assert findings() == [
        {
            "entity": ("loyalty_account", str(account.pk)),
            "check": "ledger_sum",
            "account_id": account.pk,
            "balance": 17_090,
            "ledger_sum": 16_090,
        },
        {
            "entity": ("loyalty_account", str(account.pk)),
            "check": "last_balance_after",
            "account_id": account.pk,
            "balance": 17_090,
            "last_balance_after": 16_090,
        },  # fmt: skip  # noqa: E501
    ]
    assert LoyaltyAccount.objects.get(pk=account.pk).balance == 17_090  # reported, never corrected
    assert AuditEntry.objects.filter(action="loyalty.reconciliation_mismatch").first().actor_type == "system"  # type: ignore[union-attr]
    # a second run reports it again (nothing was fixed), and the negative-balance count is live
    with connection.cursor() as cursor:
        cursor.execute("UPDATE loyalty_loyaltyaccount SET balance = -5 WHERE id = %s", [account.pk])
    again = tasks.reconcile_balances_now()
    assert (again["mismatches"], again["negative_balances"]) == (2, 1)
    assert AuditEntry.objects.filter(action="loyalty.reconciliation_completed").count() == 2


def test_broken_chain_links_and_holds_are_reported_per_row() -> None:
    account, purchase, order_id = build_chains()
    broken = LedgerEntryFactory.create(account=account, balance_after=1, at=at(10, 20), amount=10)  # C1/C2/C3
    Purchase.objects.filter(pk=purchase.purchase_id).update(cashback_amount=1)  # C5: credited amount ≠ 1
    hold = CashbackHold.objects.get(order_id=order_id)
    CashbackHold.objects.filter(pk=hold.pk).update(amount=19_999)  # C6: debit −20 000 ≠ −19 999
    summary = tasks.reconcile_balances_now()
    assert summary["checks"] == {"ledger_sum": 1, "last_balance_after": 1, "chain": 1, "purchase_links": 1, "captured_holds": 1}  # fmt: skip  # noqa: E501
    rows = {row["check"]: row for row in findings()}
    assert rows["chain"] == {"entity": ("loyalty_account", str(account.pk)), "check": "chain", "account_id": account.pk, "entry_id": broken.pk, "amount": 10, "balance_after": 1, "previous_balance_after": 16_090}  # fmt: skip  # noqa: E501
    assert rows["purchase_links"] == {"entity": ("customer", str(account.customer_id)), "check": "purchase_links", "customer_id": account.customer_id, "purchase_id": purchase.purchase_id, "cashback_amount": 1, "credited_amount": 3_140, "credited_entry_id": purchase.credited_entry_id, "reversal_entry_id": None}  # fmt: skip  # noqa: E501
    assert rows["captured_holds"] == {"entity": ("loyalty_account", str(account.pk)), "check": "captured_holds", "account_id": account.pk, "hold_id": hold.pk, "hold_amount": 19_999, "entry_kind": "debit", "entry_amount": -20_000, "entry_order_id": order_id, "order_id": order_id}  # fmt: skip  # noqa: E501
    assert LedgerEntry.objects.count() == 6 and summary["mismatches"] == 5


def test_reconciliation_is_scheduled_nightly_at_03_10_tashkent() -> None:
    from config.celery import app

    entry = app.conf.beat_schedule["loyalty-reconcile-balances"]
    assert entry["task"] == "loyalty.reconcile_balances" == tasks.reconcile_balances.name
    assert (entry["schedule"].hour, entry["schedule"].minute) == ({3}, {10})
    assert app.conf.timezone == "Asia/Tashkent"
