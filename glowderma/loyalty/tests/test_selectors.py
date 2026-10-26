"""loyalty.selectors against the contract example numbers (design 02 §4, 03 §7.7): Malika's whole timeline is
replayed through the services (totals 9 940 000 / 3 640 000 / 13 580 000, tier next remaining 6 420 000
progress 0.679, balance 5 680), plus the ledger/purchase row shapes, lifetime totals and list annotations."""

from decimal import Decimal
from typing import Any

import pytest

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import selectors, services
from glowderma.loyalty.engine import PolicyRules, TierBand
from glowderma.loyalty.models import CashbackPolicy, LedgerEntry, LoyaltyAccount, Purchase
from glowderma.loyalty.selectors import (
    AccountSummary,
    Actor,
    LedgerRow,
    NextTier,
    PurchaseRow,
    TierInfo,
    Totals,
)
from glowderma.loyalty.tests.factories import CashbackPolicyFactory, LoyaltyAccountFactory
from glowderma.loyalty.tests.support import OrderFactory, at, chain, paid_order, seed_policies

pytestmark = pytest.mark.django_db

PRE_POLICY_ORDERS = [  # (order id, created, items) — 03 §7.7, Σ 2 278 000, all before 2026-06-01
    (10012, at(4, 18, year=2025), 312_000),
    (10026, at(5, 20, year=2025), 185_000),
    (10040, at(6, 22, year=2025), 276_000),
    (10054, at(7, 24, year=2025), 156_000 + 45_000),
    (10068, at(8, 26, year=2025), 214_000),
    (10082, at(9, 28, year=2025), 298_000),
    (10096, at(11, 2, year=2025), 96_000),
    (10110, at(12, 12, year=2025), 142_000),
    (10124, at(2, 14), 174_000 + 39_000),
    (10138, at(4, 20), 185_000 + 156_000),
]
PRE_POLICY_STORE = [  # Σ 3 689 000, no credits
    ("S-60112", at(4, 18, year=2025), 1_240_000),
    ("S-61350", at(8, 3, year=2025), 985_000),
    ("S-62877", at(12, 12, year=2025), 864_000),
    ("S-63905", at(3, 7), 600_000),
]
STORE_SALES = [  # (ref, sold at, amount) under v1/v2
    ("S-64998", at(6, 1, 9, 30), 361_000),
    ("S-65230", at(6, 3, 10, 55), 1_245_000),
    ("S-68011", at(6, 20, 15, 18), 987_000),
    ("S-71840", at(7, 11, 11, 42), 1_530_000),
    ("S-76102", at(8, 2, 17, 5), 621_000),
    ("S-81377", at(9, 14, 12, 20), 864_000),
]
ADJUSTMENT_REASON = "Kassada QR o‘qilmadi, chek S-87011 qo‘lda bog‘landi"


def annotated_customers(policy: CashbackPolicy | None = None) -> Any:
    """``Customer`` rows with the list annotations (``Any``: the plugin cannot see spread annotations)."""
    return Customer.objects.annotate(**selectors.list_annotations(policy))


def replay_malika(customer: Customer, staff: Any) -> dict[str, Any]:
    """03 §7.7 in chronological order through the services; returns the results the assertions need."""
    ids: dict[str, Any] = {}
    for order_id, created, amount in PRE_POLICY_ORDERS:
        order = paid_order(customer, created_at=created, items_subtotal=amount, id=order_id)
        services.record_online_purchase(customer.pk, order.pk, amount, 0, 0, order.paid_at)
    for ref, sold, amount in PRE_POLICY_STORE:
        services.record_store_purchase(customer.pk, ref, ref, amount, sold, at=sold)
    for ref, sold, amount in STORE_SALES:
        services.record_store_purchase(customer.pk, ref, ref, amount, sold, at=sold)
    # order №10198: created 14:31, paid 14:34 (credit under v3), cancelled 16:10 → reversal
    paid_order(customer, created_at=at(10, 2, 14, 31), items_subtotal=156_000, id=10198)
    ids["10198"] = services.record_online_purchase(customer.pk, 10198, 156_000, 0, 0, at(10, 2, 14, 34))
    services.reverse_purchase(ids["10198"].purchase_id, at(10, 2, 16, 10), actor_id=staff.pk)
    sold = at(10, 9, 13, 40)
    services.record_store_purchase(customer.pk, "S-86540", "S-86540", 412_000, sold, at=sold)
    ids["adjustment"] = services.adjust(
        customer.pk, "add", 2_950, ADJUSTMENT_REASON, staff.pk, at=at(10, 12, 10, 3)
    )
    # order №10231: 554 000 (240 000 promo), 20 000 cashback, paid 15:25 → debit then credit
    o10231 = paid_order(customer, created_at=at(10, 18, 15, 20), items_subtotal=554_000, promo_subtotal=240_000, cashback_used=20_000, id=10231, pickup=True)  # fmt: skip  # noqa: E501
    services.create_hold(customer.pk, 10231, 20_000, o10231.reservation_expires_at)
    services.capture_hold(10231, at=at(10, 18, 15, 25))
    ids["10231"] = services.record_online_purchase(
        customer.pk, 10231, 554_000, 240_000, 20_000, at(10, 18, 15, 25)
    )
    sold = at(10, 21, 11, 47)
    services.record_store_purchase(customer.pk, "S-88213", "S-88213", 231_000, sold, at=sold)
    # order №10248: 808 000 (240 000 promo), 48 600 cashback, paid 14:05
    o10248 = paid_order(customer, created_at=at(10, 23, 14, 2), items_subtotal=808_000, promo_subtotal=240_000, cashback_used=48_600, id=10248)  # fmt: skip  # noqa: E501
    services.create_hold(customer.pk, 10248, 48_600, o10248.reservation_expires_at)
    services.capture_hold(10248, at=at(10, 23, 14, 5))
    ids["10248"] = services.record_online_purchase(
        customer.pk, 10248, 808_000, 240_000, 48_600, at(10, 23, 14, 5)
    )
    return ids


def test_malika_timeline_reproduces_the_contract_card() -> None:
    seed_policies()
    staff = StaffUserFactory.create(full_name="Administrator")
    malika = CustomerFactory.create(name="Malika Karimova", phone="+998901234567")
    ids = replay_malika(malika, staff)
    account = LoyaltyAccount.objects.get(customer=malika)
    # the drawn chain: 13 rows ending at 48 600, then №10248 (−48 600, +5 680)
    assert chain(account.pk) == [
        ("credit", 3_610, 3_610),
        ("credit", 12_450, 16_060),
        ("credit", 9_870, 25_930),
        ("credit", 15_300, 41_230),
        ("credit", 6_210, 47_440),
        ("credit", 8_640, 56_080),
        ("credit", 1_560, 57_640),
        ("reversal", -1_560, 56_080),
        ("credit", 4_120, 60_200),
        ("adjustment", 2_950, 63_150),
        ("debit", -20_000, 43_150),
        ("credit", 3_140, 46_290),
        ("credit", 2_310, 48_600),
        ("debit", -48_600, 0),
        ("credit", 5_680, 5_680),
    ]
    assert selectors.customer_totals(malika.pk) == Totals(store=9_940_000, online=3_640_000, lifetime=13_580_000, online_orders_count=12)  # fmt: skip  # noqa: E501
    assert selectors.account_summary(malika.pk) == AccountSummary(balance=5_680, held=0, available=5_680)
    with pinned_clock(at(10, 24, 12, 5)):
        assert selectors.tier_for(13_580_000) == TierInfo(policy_version=3, percent=Decimal("1.00"), next=NextTier(percent=Decimal("2.00"), from_amount=20_000_000, remaining=6_420_000, progress=0.679))  # fmt: skip  # noqa: E501
    assert selectors.lifetime_totals([malika.pk, 10**6]) == {malika.pk: 13_580_000, 10**6: 0}
    # purchases: 10 + 4 pre-policy, 8 credited store sales, №10198 (reversed), №10231, №10248
    purchases = selectors.purchases_for_customer(malika.pk)
    assert len(purchases) == 25 and Purchase.objects.filter(customer=malika, reversed=True).count() == 1
    assert purchases[0] == PurchaseRow(id=ids["10248"].purchase_id, at=at(10, 23, 14, 5), source="online", order_id=10248, billz_sale_ref=None, amount=808_000, promo_amount=240_000, cashback=5_680, policy_version=3, reversed=False)  # fmt: skip  # noqa: E501
    by_order = {p.order_id: p for p in purchases if p.order_id}
    assert selectors.purchase_for_order(10248) == purchases[0] and selectors.purchase_for_order(10999) is None
    assert selectors.purchase_for_order(10198) == by_order[10198]
    assert (by_order[10198].reversed, by_order[10198].cashback, by_order[10198].policy_version) == (
        True,
        1_560,
        3,
    )
    assert (by_order[10231].cashback, by_order[10231].promo_amount) == (3_140, 240_000)
    assert all(p.policy_version is None and p.cashback == 0 for p in purchases if p.at < at(6, 1))
    credited_store = [p.policy_version for p in purchases if p.source == "store_qr" and p.cashback]
    assert credited_store == [3, 3, 2, 1, 1, 1, 1, 1]
    # №10248's inputs as the engine saw them
    assert (ids["10248"].lifetime_before, ids["10248"].base, ids["10248"].percent) == (12_772_000, 568_000, Decimal("1.00"))  # fmt: skip  # noqa: E501
    assert (ids["10231"].base, ids["10231"].promo_excluded, ids["10231"].cashback_amount) == (
        314_000,
        True,
        3_140,
    )
    # ledger rows in the contract shape, newest first
    ledger = selectors.ledger_for_customer(malika.pk)
    assert len(ledger) == 15 and [row.amount for row in ledger[:2]] == [5_680, -48_600]
    assert ledger[0] == LedgerRow(id=ledger[0].id, at=at(10, 23, 14, 5), kind="credit", amount=5_680, balance_after=5_680, source_type="order", source_ref="10248", order_id=10248, base=568_000, percent=Decimal("1.00"), promo_excluded=True, policy_version=3, reverses_id=None, reason=None, actor=None)  # fmt: skip  # noqa: E501
    s88213 = next(row for row in ledger if row.source_ref == "S-88213")
    assert (s88213.at, s88213.kind, s88213.amount, s88213.balance_after, s88213.source_type, s88213.order_id, s88213.base, s88213.percent, s88213.promo_excluded, s88213.policy_version) == (at(10, 21, 11, 47), "credit", 2_310, 48_600, "billz_sale", None, 231_000, Decimal("1.00"), False, 3)  # fmt: skip  # noqa: E501
    adjustment = next(row for row in ledger if row.kind == "adjustment")
    assert (adjustment.source_type, adjustment.source_ref, adjustment.order_id, adjustment.reason, adjustment.actor, adjustment.balance_after) == ("admin", None, None, ADJUSTMENT_REASON, Actor(staff.pk, "Administrator"), 63_150)  # fmt: skip  # noqa: E501
    reversal = next(row for row in ledger if row.kind == "reversal")
    credit_10198 = next(row for row in ledger if row.order_id == 10198 and row.kind == "credit")
    assert (reversal.reverses_id, reversal.amount, reversal.source_ref, reversal.base, reversal.policy_version, reversal.actor) == (credit_10198.id, -1_560, "10198", None, None, None)  # fmt: skip  # noqa: E501
    assert selectors.ledger_for_order(10248) == [row for row in reversed(ledger) if row.order_id == 10248]
    assert [(r.kind, r.amount) for r in selectors.ledger_for_order(10231)] == [
        ("debit", -20_000),
        ("credit", 3_140),
    ]
    assert [(r.kind, r.amount) for r in selectors.ledger_for_order(10198)] == [
        ("credit", 1_560),
        ("reversal", -1_560),
    ]
    assert selectors.ledger_for_order(10012) == [] and selectors.ledger_entry(ledger[0].id) == ledger[0]
    # the list annotations agree with the card
    row = annotated_customers().values("lifetime_total", "balance", "percent", "online_orders_count").get(pk=malika.pk)  # fmt: skip  # noqa: E501
    assert row == {"lifetime_total": 13_580_000, "balance": 5_680, "percent": Decimal("1.00"), "online_orders_count": 12}  # fmt: skip  # noqa: E501
    assert LedgerEntry.objects.count() == 15 and CashbackPolicy.objects.count() == 3


def test_account_summary_and_totals_without_rows() -> None:
    customer = CustomerFactory.create()
    assert selectors.account_summary(customer.pk) == AccountSummary(0, 0, 0)
    assert selectors.customer_totals(customer.pk) == Totals(0, 0, 0, 0)
    assert (
        selectors.ledger_for_customer(customer.pk) == []
        and selectors.purchases_for_customer(customer.pk) == []
    )
    assert selectors.lifetime_totals(set()) == {} and selectors.held_amount(10**9) == 0
    account = LoyaltyAccountFactory.create(customer=customer, balance=-700)
    assert selectors.account_summary(customer.pk) == AccountSummary(-700, 0, -700)
    assert account.balance == -700


def test_tier_for_vectors() -> None:
    assert selectors.tier_for(13_580_000) is None  # no policy at all
    seed_policies()
    with pinned_clock(at(10, 24)):
        assert selectors.tier_for(13_580_000).next == NextTier(Decimal("2.00"), 20_000_000, 6_420_000, 0.679)  # type: ignore[union-attr]
        assert selectors.tier_for(35_000_000) == TierInfo(3, Decimal("3.00"), None)
        assert selectors.tier_for(50_000_000) is None  # bounded last tier
        assert selectors.tier_for(0) == TierInfo(3, Decimal("1.00"), NextTier(Decimal("2.00"), 20_000_000, 20_000_000, 0.0))  # fmt: skip  # noqa: E501
        assert selectors.tier_for(29_999_999).next == NextTier(Decimal("3.00"), 30_000_000, 1, 1.0)  # type: ignore[union-attr]
    with pinned_clock(at(7, 1)):  # v1: a single bounded tier → no next
        assert selectors.tier_for(1_000) == TierInfo(1, Decimal("1.00"), None)
    with pinned_clock(at(5, 1)):
        assert selectors.tier_for(1_000) is None
    v2 = CashbackPolicy.objects.get(version=2)
    assert selectors.tier_for(25_000_000, v2) == TierInfo(2, Decimal("2.00"), None)
    rules = PolicyRules("before_purchase", False, (TierBand(0, 100, Decimal("0.5")), TierBand(100, None, Decimal("1"))), version=9)  # fmt: skip  # noqa: E501
    assert selectors.tier_for(25, rules) == TierInfo(
        9, Decimal("0.50"), NextTier(Decimal("1.00"), 100, 75, 0.25)
    )
    assert selectors.tier_for(25, PolicyRules("before_purchase", False, (), version=None)) is None


def test_list_annotations_order_by_lifetime_and_balance_in_sql() -> None:
    seed_policies()
    staff = StaffUserFactory.create()
    rich, mid, poor = CustomerFactory.create(), CustomerFactory.create(), CustomerFactory.create()
    services.record_store_purchase(
        rich.pk, "S-A", "S-A", 25_000_000, at(10, 2), at=at(10, 2)
    )  # v3 1 % → 250 000
    order = paid_order(mid, created_at=at(10, 3), items_subtotal=2_000_000)
    services.record_online_purchase(mid.pk, order.pk, 2_000_000, 0, 0, order.paid_at)  # → 20 000
    reversed_order = paid_order(mid, created_at=at(10, 4), items_subtotal=9_000_000)
    reversed_purchase = services.record_online_purchase(mid.pk, reversed_order.pk, 9_000_000, 0, 0, reversed_order.paid_at)  # fmt: skip  # noqa: E501
    services.reverse_purchase(reversed_purchase.purchase_id, at(10, 5))
    services.adjust(poor.pk, "add", 300_000, "sovg‘a", staff.pk)
    with pinned_clock(at(10, 24)):
        annotated = annotated_customers().filter(pk__in=[rich.pk, mid.pk, poor.pk])
    rows = {row["id"]: row for row in annotated.values("id", "lifetime_total", "balance", "percent", "online_orders_count")}  # fmt: skip  # noqa: E501
    assert rows[rich.pk] == {"id": rich.pk, "lifetime_total": 25_000_000, "balance": 250_000, "percent": Decimal("2.00"), "online_orders_count": 0}  # fmt: skip  # noqa: E501
    assert rows[mid.pk] == {"id": mid.pk, "lifetime_total": 2_000_000, "balance": 20_000, "percent": Decimal("1.00"), "online_orders_count": 1}  # fmt: skip  # noqa: E501
    assert rows[poor.pk] == {"id": poor.pk, "lifetime_total": 0, "balance": 300_000, "percent": Decimal("1.00"), "online_orders_count": 0}  # fmt: skip  # noqa: E501
    assert list(annotated.order_by("-lifetime_total", "id").values_list("id", flat=True)) == [
        rich.pk,
        mid.pk,
        poor.pk,
    ]
    assert list(annotated.order_by("-balance", "id").values_list("id", flat=True)) == [
        poor.pk,
        rich.pk,
        mid.pk,
    ]
    assert list(annotated.order_by("balance", "id").values_list("id", flat=True)) == [
        mid.pk,
        rich.pk,
        poor.pk,
    ]
    # a customer without any row still sorts (COALESCE 0) and sits in the first tier; no policy → percent
    # null; a bounded top → null
    none = CustomerFactory.create()
    with pinned_clock(at(10, 24)):
        row = annotated_customers().values("lifetime_total", "balance", "percent").get(pk=none.pk)  # fmt: skip  # noqa: E501
    assert row == {"lifetime_total": 0, "balance": 0, "percent": Decimal("1.00")}
    with pinned_clock(at(5, 1)):  # before any policy
        assert annotated_customers().values("percent").get(pk=rich.pk) == {"percent": None}  # fmt: skip  # noqa: E501
    bounded = CashbackPolicyFactory.create(created_at=at(10, 25), effective_from=at(10, 26), tiers=[(0, 1_000_000, "1.00")])  # fmt: skip  # noqa: E501
    assert annotated_customers(bounded).values("percent").get(pk=rich.pk) == {"percent": None}  # fmt: skip  # noqa: E501
    assert selectors.lifetime_totals((rich.pk, mid.pk, poor.pk)) == {rich.pk: 25_000_000, mid.pk: 2_000_000, poor.pk: 0}  # fmt: skip  # noqa: E501


def test_account_summary_is_one_statement(django_assert_num_queries: Any) -> None:
    account = LoyaltyAccountFactory.create(balance=50_000)
    for amount in (20_000, 5_000):
        order = OrderFactory.create(customer=account.customer, cashback_used=amount)
        services.create_hold(account.customer_id, order.pk, amount, order.reservation_expires_at)
    released = OrderFactory.create(customer=account.customer, cashback_used=1_000)
    services.create_hold(account.customer_id, released.pk, 1_000, released.reservation_expires_at)
    services.release_hold(released.pk, "order_cancelled")
    with django_assert_num_queries(1):
        summary = selectors.account_summary(account.customer_id)
    assert summary == AccountSummary(balance=50_000, held=25_000, available=25_000)
    no_account = CustomerFactory.create()
    with django_assert_num_queries(1):
        assert selectors.account_summary(no_account.pk) == AccountSummary(0, 0, 0)


# --- per-order hold and the demo-seeder reads ------------------------------------------------------------


def test_hold_for_order_prefers_the_active_hold_else_the_newest() -> None:
    from glowderma.loyalty.selectors import HoldView
    from glowderma.loyalty.tests.factories import CashbackHoldFactory

    released = CashbackHoldFactory.create(released=True)
    order_id = released.order_id
    assert selectors.hold_for_order(order_id) == HoldView(
        id=released.pk, amount=released.amount, state="released", expires_at=released.expires_at
    )
    active = CashbackHoldFactory.create(order=released.order, account=released.account)
    view = selectors.hold_for_order(order_id)
    assert view is not None and (view.id, view.state) == (active.pk, "active")
    later = CashbackHoldFactory.create(order=released.order, account=released.account, released=True)
    assert later.pk > active.pk
    view = selectors.hold_for_order(order_id)
    assert view is not None and view.id == active.pk  # the active hold wins over a newer released one
    assert selectors.hold_for_order(order_id + 1_000_000) is None


def test_seeder_reads_store_sales_adjustments_and_the_ledger() -> None:
    seed_policies()
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    assert not selectors.has_ledger_entries()
    assert selectors.store_sale_ids(customer.pk) == set()
    with pinned_clock(at(9, 5)):
        services.record_store_purchase(customer.pk, "S-1", "S-1", 231_000, at(9, 5))
        services.adjust(customer.pk, "add", 2_950, "Kassada QR o‘qilmadi", staff.pk)
    assert selectors.store_sale_ids(customer.pk) == {"S-1"}
    assert selectors.has_adjustment(customer.pk, "Kassada QR o‘qilmadi")
    assert not selectors.has_adjustment(customer.pk, "boshqa sabab")
    assert selectors.has_ledger_entries()
    assert selectors.ledger_table() == LedgerEntry._meta.db_table


@pytest.mark.django_db(transaction=True)
def test_lock_account_creates_the_account_and_returns_the_newest_instant() -> None:
    from django.db import transaction

    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    with transaction.atomic():
        assert services.lock_account(customer.pk) is None
    assert LoyaltyAccount.objects.filter(customer=customer).exists()
    with pinned_clock(at(9, 5)):
        services.adjust(customer.pk, "add", 1_000, "Tuzatish sababi", staff.pk)
    with transaction.atomic():
        assert services.lock_account(customer.pk) == at(9, 5)
