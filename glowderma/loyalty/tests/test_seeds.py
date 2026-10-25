"""loyalty.seed (design 03 §7.7, 02 §1.1): the three policy versions, pinned timestamps, derived states at the
anchor, idempotency, the demo admin as author and the standalone run (author NULL)."""

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from glowderma.accounts import seeds as accounts_seeds
from glowderma.accounts.seeds import CustomerFacts
from glowderma.core import seeding
from glowderma.core.seeding import SeedContext, discover_seeders
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import seeds
from glowderma.loyalty.models import CashbackPolicy, CashbackTier, LedgerEntry, LoyaltyAccount, Purchase
from glowderma.loyalty.seeds import POLICY_VERSIONS, STORE_SALE_SOLD_AT, seed_loyalty

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)

pytestmark = pytest.mark.django_db


def at(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TASHKENT)


def test_seeder_registered_with_order_60(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    importlib.reload(seeds)
    assert [(name, order) for name, order, _ in seeding.registered_seeders()] == [("loyalty", 60)]
    assert "glowderma.loyalty.seeds" in discover_seeders()


def test_design_vector() -> None:
    assert [
        (s.version, s.created_at, s.effective_from, s.by_admin, s.earn_on_cashback_paid)
        for s in POLICY_VERSIONS
    ] == [  # fmt: skip  # noqa: E501
        (1, at(5, 20, 10), at(6, 1), False, False),
        (2, at(8, 1, 10), at(8, 15), True, False),
        (3, at(9, 20, 10), at(10, 1), True, True),  # G-2
    ]
    assert all(s.basis == "before_purchase" for s in POLICY_VERSIONS)
    assert [s.tiers for s in POLICY_VERSIONS] == [
        ((0, 20_000_000, "1.00"),),
        ((0, 20_000_000, "1.00"), (20_000_000, 30_000_000, "2.00")),
        ((0, 20_000_000, "1.00"), (20_000_000, 30_000_000, "2.00"), (30_000_000, 50_000_000, "3.00")),
    ]


def test_seed_reproduces_the_design_with_the_demo_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    admin = accounts_seeds.ensure_admin()
    assert admin.pk == 1 and admin.email == "admin@glowderma.uz"
    seed_loyalty(CONTEXT)
    assert CashbackPolicy.objects.count() == 3 and CashbackTier.objects.count() == 6
    rows = {p.version: p for p in CashbackPolicy.objects.with_derived()}
    v1, v2, v3 = rows[1], rows[2], rows[3]
    assert (v1.created_at, v1.effective_from, v1.author, v1.earn_on_cashback_paid) == (at(5, 20, 10), at(6, 1), None, False)  # fmt: skip  # noqa: E501
    assert (v2.created_at, v2.effective_from, v2.author_id, v2.earn_on_cashback_paid) == (at(8, 1, 10), at(8, 15), 1, False)  # fmt: skip  # noqa: E501
    assert (v3.created_at, v3.effective_from, v3.author_id, v3.earn_on_cashback_paid) == (at(9, 20, 10), at(10, 1), 1, True)  # fmt: skip  # noqa: E501
    assert {p.basis for p in rows.values()} == {"before_purchase"}
    assert [(t.from_amount, t.to_amount, t.percent) for t in v3.tiers.all()] == [
        (0, 20_000_000, Decimal("1.00")),
        (20_000_000, 30_000_000, Decimal("2.00")),
        (30_000_000, 50_000_000, Decimal("3.00")),
    ]
    assert [t.to_amount for t in v1.tiers.all()] == [20_000_000]
    assert [t.to_amount for t in v2.tiers.all()] == [20_000_000, 30_000_000]
    # tiers are inserted sorted by ``from``, so id order equals ``from`` order within a version (02 §1.2)
    assert list(v3.tiers.order_by("id").values_list("from_amount", flat=True)) == [0, 20_000_000, 30_000_000]
    assert all(t.created_at == t.policy.created_at for t in CashbackTier.objects.select_related("policy"))
    # derived (03 §7.7): v1 "1-iyun – 14-avg 2026" expired, v2 "15-avg – 30-sen 2026" expired, v3 "1-okt dan"
    assert [p.superseded_by for p in (v1, v2, v3)] == [None, None, None]
    assert [p.effective_to for p in (v1, v2, v3)] == [at(8, 15), at(10, 1), None]
    assert [p.state_at(ANCHOR) for p in (v1, v2, v3)] == ["expired", "expired", "active"]
    assert CashbackPolicy.objects.active_at(at(5, 31)) is None  # pre-policy purchases: policy null
    assert CashbackPolicy.objects.active_at(at(6, 1, 9, 30)) == v1  # S-64998
    assert CashbackPolicy.objects.active_at(at(9, 14, 12, 20)) == v2  # S-81377
    assert CashbackPolicy.objects.active_at(at(10, 2, 14, 34)) == v3  # order №10198
    assert CashbackPolicy.objects.active_at(ANCHOR) == v3
    # customers whose store sales were seeded on 2026-09-05 earn under v2 (1 % below 20 M)
    assert CashbackPolicy.objects.active_at(at(9, 5, 12)) == v2


def test_seed_is_idempotent_and_runs_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not get_user_model().objects.exists()
    seed_loyalty(CONTEXT)  # no accounts.seed before: the authors are NULL ("Tizim")
    assert list(CashbackPolicy.objects.order_by("version").values_list("author", flat=True)) == [None] * 3
    first = list(CashbackPolicy.objects.order_by("version").values_list("id", "created_at"))
    tiers = list(CashbackTier.objects.order_by("id").values_list("id", "policy_id", "from_amount"))
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    accounts_seeds.ensure_admin()
    seed_loyalty(CONTEXT)  # a later run never rewrites an existing version (append-only rows anyway)
    seed_loyalty(SeedContext(anchor=ANCHOR, keep_open=False))
    assert list(CashbackPolicy.objects.order_by("version").values_list("id", "created_at")) == first
    assert list(CashbackTier.objects.order_by("id").values_list("id", "policy_id", "from_amount")) == tiers
    assert list(CashbackPolicy.objects.order_by("version").values_list("author", flat=True)) == [None] * 3
    assert CashbackPolicy.objects.count() == 3 and CashbackTier.objects.count() == 6
    assert not Purchase.objects.exists()  # no customers → no store sales, no error


def test_seed_fills_only_the_missing_versions() -> None:
    seeds._insert(POLICY_VERSIONS[0], None)
    seed_loyalty(CONTEXT)
    assert list(CashbackPolicy.objects.order_by("version").values_list("version", flat=True)) == [1, 2, 3]
    assert CashbackTier.objects.count() == 6


# --- store sales of the generated customers (03 §7.7) ---


def test_store_sales_vector() -> None:
    sales = seeds.store_sales()
    ids = [customer_id for customer_id, _, _ in sales]
    assert 5 not in ids and 7 in ids and all(1 <= customer_id <= 426 for customer_id in ids)
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert all(sale_id == f"S-{80_000 + customer_id * 7}" for customer_id, sale_id, _ in sales)
    assert all(
        amount % 50_000 == 0 and 0 < amount <= 3_000_000
        for customer_id, _, amount in sales
        if customer_id != 7
    )
    assert len(sales) == sum(1 for c in range(1, 427) if c != 5 and seeds.store_total(c) > 0)
    assert seeds.store_total(7) == max(CustomerFacts(7).store_total, 2_000_000) >= 2_000_000
    assert all(seeds.store_total(c) == CustomerFacts(c).store_total for c in range(1, 427) if c != 7)
    assert seeds.store_sale_id(5) == "S-80035" and STORE_SALE_SOLD_AT == at(9, 5, 12)


def test_seed_records_the_store_sales_through_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    # the standalone run: once the commerce seeder (B5) is registered it replays the store sales instead
    monkeypatch.setattr(
        seeding, "_registry", {name: row for name, row in seeding._registry.items() if name != "commerce"}
    )
    accounts_seeds.seed_accounts(CONTEXT)
    seed_loyalty(CONTEXT)
    sales = seeds.store_sales()
    assert (
        len(sales)
        == Purchase.objects.count()
        == LedgerEntry.objects.count()
        == LoyaltyAccount.objects.count()
    )
    assert not Purchase.objects.filter(customer_id=5).exists()  # Malika's timeline belongs to B5
    assert set(LoyaltyAccount.objects.values_list("customer_id", flat=True)) == {c for c, _, _ in sales}
    # Javohir (7): max(store, 2 000 000) so order №10246 can spend 20 000
    javohir = Purchase.objects.select_related("policy").get(customer_id=7)
    assert (javohir.billz_sale_id, javohir.amount) == (
        "S-80049",
        max(CustomerFacts(7).store_total, 2_000_000),
    )
    assert LoyaltyAccount.objects.get(customer_id=7).balance == javohir.amount // 100 >= 20_000
    # a generated customer: v2 (sold 2026-09-05), credit store // 100 at 1 %, every timestamp pinned
    customer_id, sale_id, amount = next(row for row in sales if row[0] >= 13)
    purchase = Purchase.objects.select_related("policy", "credited_entry").get(billz_sale_id=sale_id)
    assert purchase.policy is not None and purchase.credited_entry is not None
    assert (purchase.customer_id, purchase.billz_sale_ref, purchase.amount, purchase.source, purchase.cashback_paid) == (customer_id, sale_id, amount, "store_qr", 0)  # fmt: skip  # noqa: E501
    assert (purchase.at, purchase.created_at, purchase.updated_at, purchase.policy.version, purchase.lifetime_before) == (STORE_SALE_SOLD_AT, STORE_SALE_SOLD_AT, STORE_SALE_SOLD_AT, 2, 0)  # fmt: skip  # noqa: E501
    assert purchase.cashback_amount == amount // 100 and not purchase.reversed
    entry = purchase.credited_entry
    assert (entry.at, entry.amount, entry.balance_after, entry.percent, entry.base, entry.source_type, entry.source_ref, entry.promo_excluded) == (STORE_SALE_SOLD_AT, amount // 100, amount // 100, Decimal("1.00"), amount, "billz_sale", sale_id, False)  # fmt: skip  # noqa: E501
    assert {p.policy_id for p in Purchase.objects.all()} == {CashbackPolicy.objects.get(version=2).pk}
    assert not LoyaltyAccount.objects.filter(balance__lt=0).exists()
    assert all(
        a.balance == a.entries.get().amount for a in LoyaltyAccount.objects.prefetch_related("entries")
    )
    # idempotent: a second run (with either keep_open) adds nothing and rewrites nothing
    before = list(Purchase.objects.order_by("id").values_list("id", "billz_sale_id", "cashback_amount", "updated_at"))  # fmt: skip  # noqa: E501
    seed_loyalty(CONTEXT)
    seed_loyalty(SeedContext(anchor=ANCHOR, keep_open=False))
    assert list(Purchase.objects.order_by("id").values_list("id", "billz_sale_id", "cashback_amount", "updated_at")) == before  # fmt: skip  # noqa: E501
    assert LedgerEntry.objects.count() == len(sales) and seeds.seed_store_sales() == 0


def test_store_sales_join_the_commerce_replay_once_it_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a ``commerce`` seeder registered, seed_loyalty leaves the store sales to its chronological replay
    (post_entry refuses entries older than an account's newest one), which calls seed_store_sales itself."""
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    accounts_seeds.seed_accounts(CONTEXT)
    monkeypatch.setattr(seeding, "_registry", {"commerce": (70, lambda context: None)})
    seed_loyalty(CONTEXT)
    assert CashbackPolicy.objects.count() == 3 and not Purchase.objects.exists()
    assert seeds.seed_store_sales() == len(seeds.store_sales()) == Purchase.objects.count()
    assert seeds.seed_store_sales() == 0
    assert set(LedgerEntry.objects.values_list("at", flat=True)) == {STORE_SALE_SOLD_AT}
