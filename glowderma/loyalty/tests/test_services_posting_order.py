"""loyalty.services — posting order and argument guards: an entry is never older than the account's newest one
(explicit ``at`` refused, the default instant read under the lock), reconciliation follows ``id`` order, naive
datetimes and over-long Billz references are refused before any DB work."""

import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from threading import Event, Thread
from typing import Any

import pytest
from django.db import connection, transaction

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import services, tasks
from glowderma.loyalty.engine import TierBand
from glowderma.loyalty.models import LedgerEntry, LoyaltyAccount, Purchase
from glowderma.loyalty.tests.factories import LedgerEntryFactory, LoyaltyAccountFactory
from glowderma.loyalty.tests.support import at, chain, paid_order, seed_policies

pytestmark = pytest.mark.django_db

CLEAN = {"ledger_sum": 0, "last_balance_after": 0, "chain": 0, "purchase_links": 0, "captured_holds": 0}


def by_business_time(account_id: int) -> list[tuple[int, int]]:
    """``(amount, balance_after)`` newest-last by ``(at, id)`` — the order the customer card reads."""
    rows = LedgerEntry.objects.filter(account_id=account_id).order_by("at", "id")
    return list(rows.values_list("amount", "balance_after"))


def test_backdated_online_purchase_after_a_later_store_sale_is_refused() -> None:
    """Probe P1: a September store credit, then an August order replayed later."""
    seed_policies()
    customer = CustomerFactory.create()
    services.record_store_purchase(customer.pk, "S-1", "S-1", 500_000, at(9, 5, 12), at=at(9, 5, 12))
    order = paid_order(customer, created_at=at(8, 20, 10), items_subtotal=3_000_000)
    with pytest.raises(services.OutOfOrderPosting) as info:
        services.record_online_purchase(customer.pk, order.pk, 3_000_000, 0, 0, at(8, 20, 10, 3))
    assert isinstance(info.value, ValueError)
    assert (info.value.at, info.value.latest_at) == (at(8, 20, 10, 3), at(9, 5, 12))
    # the whole purchase rolled back: no purchase row, no lifetime counted, the chain untouched
    assert not Purchase.objects.filter(order_id=order.pk).exists()
    account = LoyaltyAccount.objects.get(customer=customer)
    assert chain(account.pk) == [("credit", 5_000, 5_000)] and account.balance == 5_000
    assert tasks.reconcile_balances_now()["checks"] == CLEAN


def test_backdated_adjustments_and_store_credits_are_refused() -> None:
    seed_policies()
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    services.adjust(customer.pk, "add", 1_000, "birinchi", staff.pk, at=at(10, 20))
    with pytest.raises(services.OutOfOrderPosting):  # probe (a)
        services.adjust(customer.pk, "add", 500, "ikkinchi", staff.pk, at=at(10, 19))
    services.adjust(
        customer.pk, "add", 500, "bir vaqtda", staff.pk, at=at(10, 20)
    )  # the same instant is fine
    with pinned_clock(at(10, 24)):
        services.adjust(customer.pk, "add", 250, "hozir", staff.pk)  # default instant = now
    with pytest.raises(services.OutOfOrderPosting):  # probe (b): store credit posted before "now"
        services.record_store_purchase(customer.pk, "S-2", "S-2", 300_000, at(10, 1), at=at(10, 1))
    assert not Purchase.objects.exists()
    account = LoyaltyAccount.objects.get(customer=customer)
    assert by_business_time(account.pk) == [(1_000, 1_000), (500, 1_500), (250, 1_750)]
    with pytest.raises(services.OutOfOrderPosting):
        services.post_entry(account.pk, kind="adjustment", amount=1, source_type="admin", reason="x", actor_id=staff.pk, at=at(10, 23))  # fmt: skip  # noqa: E501
    assert tasks.reconcile_balances_now()["mismatches"] == 0


def test_default_instant_never_goes_behind_the_newest_entry() -> None:
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    services.adjust(customer.pk, "add", 1_000, "kelajak", staff.pk, at=at(12, 1))
    with pinned_clock(at(10, 24)):  # the clock is behind the newest entry (skew)
        later = services.adjust(customer.pk, "subtract", 100, "hozir", staff.pk)
    entry = LedgerEntry.objects.get(pk=later.entry_id)
    assert entry.at == at(12, 1)  # clamped to the newest entry, not stamped before it
    assert by_business_time(later.account_id) == [(1_000, 1_000), (-100, 900)]


@pytest.mark.django_db(transaction=True)
def test_default_instant_is_read_after_the_account_lock() -> None:
    """Probe P5: a post_entry(at=None) that waits on the lock is never stamped before the holder's entry."""
    account = LoyaltyAccountFactory.create(balance=0)
    staff_id = StaffUserFactory.create().pk
    locked, waiting, done = Event(), Event(), Event()
    failures: list[BaseException] = []

    def holder() -> None:
        try:
            with transaction.atomic():
                LoyaltyAccount.objects.select_for_update().get(pk=account.pk)
                locked.set()
                assert waiting.wait(timeout=10)
                services.post_entry(account.pk, kind="adjustment", amount=100, source_type="admin", reason="holder", actor_id=staff_id)  # fmt: skip  # noqa: E501
        except BaseException as exc:  # noqa: BLE001 - surfaced through ``failures``
            failures.append(exc)
        finally:
            connection.close()

    def waiter() -> None:
        try:
            assert locked.wait(timeout=10)
            services.post_entry(account.pk, kind="adjustment", amount=7, source_type="admin", reason="waiter", actor_id=staff_id)  # fmt: skip  # noqa: E501
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            done.set()
            connection.close()

    threads = [Thread(target=holder, daemon=True), Thread(target=waiter, daemon=True)]
    for thread in threads:
        thread.start()
    assert locked.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # wait until the waiter is blocked on the row lock
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = current_database()"
            )
            if cursor.fetchone()[0]:
                break
        time.sleep(0.02)
    waiting.set()
    assert done.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=10)
    assert failures == []
    entries = list(LedgerEntry.objects.filter(account_id=account.pk).order_by("id"))
    assert [(e.reason, e.amount, e.balance_after) for e in entries] == [
        ("holder", 100, 100),
        ("waiter", 7, 107),
    ]
    assert entries[0].at <= entries[1].at  # business time follows posting order
    assert by_business_time(account.pk) == [(100, 100), (7, 107)]
    assert tasks.reconcile_balances_now()["checks"] == CLEAN


def test_reconciliation_follows_posting_order_not_business_time() -> None:
    """C2/C3 read the chain in ``id`` order: rows written out of ``at`` order (bypassing the guard, e.g. data
    from before it) with a correct running sum are not false mismatches."""
    account = LoyaltyAccountFactory.create(balance=0)
    LedgerEntryFactory.create(account=account, adjustment=True, amount=1_000, at=at(10, 20))
    LedgerEntryFactory.create(account=account, adjustment=True, amount=500, at=at(10, 19))
    assert chain(account.pk) == [("adjustment", 1_000, 1_000), ("adjustment", 500, 1_500)]
    assert by_business_time(account.pk) == [(500, 1_500), (1_000, 1_000)]
    summary = tasks.reconcile_balances_now()
    assert summary["mismatches"] == 0 and summary["checks"] == CLEAN


# --- argument guards ---

NAIVE = datetime(2026, 10, 20, 12, 0)  # noqa: DTZ001 - the point of the test
AWARE = at(10, 20, 12)
TIERS = (TierBand(0, None, Decimal("1.00")),)

NAIVE_CALLS: dict[str, Callable[[], Any]] = {
    "post_entry": lambda: services.post_entry(1, kind="adjustment", amount=1, source_type="admin", at=NAIVE),
    "create_hold": lambda: services.create_hold(1, 1, 1, NAIVE),
    "capture_hold": lambda: services.capture_hold(1, at=NAIVE),
    "release_hold": lambda: services.release_hold(1, "order_cancelled", at=NAIVE),
    "expire_holds": lambda: services.expire_holds(NAIVE),
    "record_online_purchase": lambda: services.record_online_purchase(1, 1, 100, 0, 0, NAIVE),
    "record_store_purchase.sold_at": lambda: services.record_store_purchase(1, "S-1", "S-1", 100, NAIVE),
    "record_store_purchase.at": lambda: services.record_store_purchase(1, "S-1", "S-1", 100, AWARE, at=NAIVE),
    "reverse_purchase": lambda: services.reverse_purchase(1, NAIVE),
    "link_billz_sale": lambda: services.link_billz_sale(1, pushed_at=NAIVE),
    "adjust": lambda: services.adjust(1, "add", 1, "sabab", 1, at=NAIVE),
    "create_policy_version": lambda: services.create_policy_version(
        NAIVE, "before_purchase", True, TIERS, None
    ),
    "validate_policy_version.effective_from": lambda: services.validate_policy_version(
        NAIVE, TIERS, now=AWARE
    ),
    "validate_policy_version.now": lambda: services.validate_policy_version(AWARE, TIERS, now=NAIVE),
}


@pytest.mark.parametrize("call", NAIVE_CALLS.values(), ids=NAIVE_CALLS.keys())
def test_naive_datetimes_are_refused_before_any_db_work(
    call: Callable[[], Any], django_assert_num_queries: Any
) -> None:
    with django_assert_num_queries(0), pytest.raises(ValueError, match="must be timezone-aware"):
        call()


def test_billz_reference_lengths_are_checked_up_front(django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(0):
        with pytest.raises(ValueError, match="billz_sale_ref is longer than 32"):
            services.record_store_purchase(1, "x" * 40, "", 100, AWARE)  # the ref would default to the id
        with pytest.raises(ValueError, match="billz_sale_ref is longer than 32"):
            services.record_store_purchase(1, "S-1", "r" * 33, 100, AWARE)
        with pytest.raises(ValueError, match="billz_sale_id is longer than 64"):
            services.record_store_purchase(1, "x" * 65, "S-1", 100, AWARE)
        with pytest.raises(ValueError, match="billz_sale_ref is longer than 32"):
            services.link_billz_sale(1, billz_sale_ref="r" * 33)
        with pytest.raises(ValueError, match="billz_sale_id is longer than 64"):
            services.link_billz_sale(1, billz_sale_id="x" * 65)
    seed_policies()
    customer = CustomerFactory.create()
    long_id = "x" * 40
    stored = services.record_store_purchase(customer.pk, long_id, "S-40", 100_000, AWARE, at=AWARE)
    assert (stored.billz_sale_id, stored.billz_sale_ref) == (long_id, "S-40")
    order = paid_order(customer, created_at=at(10, 21), items_subtotal=100_000)
    online = services.record_online_purchase(customer.pk, order.pk, 100_000, 0, 0, at(10, 21, 0, 3))
    with pytest.raises(ValueError, match="billz_sale_ref is longer than 32"):  # defaulted ref = 33-char id
        services.link_billz_sale(online.purchase_id, billz_sale_id="y" * 33)
    linked = services.link_billz_sale(online.purchase_id, billz_sale_id="y" * 33, billz_sale_ref="S-41")
    assert (linked.billz_sale_id, linked.billz_sale_ref, linked.changed) == ("y" * 33, "S-41", True)
    assert enums.PurchaseSource.ONLINE == Purchase.objects.get(pk=online.purchase_id).source
