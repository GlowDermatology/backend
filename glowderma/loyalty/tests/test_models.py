"""loyalty models (design 02 §1, 03 §10): every CHECK / unique / partial unique through IntegrityError, the FK
``on_delete`` rules, ordering, ``__str__``, the derived policy state over time, the indexes/constraints as
PostgreSQL stores them and the nightly reconciliation checks (02 §1.9.3) over factory-built chains."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, models, transaction
from django.db.models import ProtectedError

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.models import AppendOnlyError
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty.models import (
    CashbackPolicy,
    CashbackTier,
    LedgerEntry,
    LoyaltyAccount,
    PosDevice,
    Purchase,
    PurchaseItem,
)
from glowderma.loyalty.tests.factories import (
    ORDER_FACTORY,
    PRODUCT_FACTORY,
    CashbackHoldFactory,
    CashbackPolicyFactory,
    CashbackTierFactory,
    LedgerEntryFactory,
    LoyaltyAccountFactory,
    PosDeviceFactory,
    PurchaseFactory,
    PurchaseItemFactory,
    reverse_entry,
    reverse_purchase_rows,
    upper_factory,
)

pytestmark = pytest.mark.django_db

# upper-layer rows the FKs point at (see ``factories.upper_factory``); ``Order`` itself only for its manager
Order = apps.get_model("commerce", "Order")
OrderFactory = upper_factory(ORDER_FACTORY)
ProductFactory = upper_factory(PRODUCT_FACTORY)


def at(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraint_names(table: str, types: str = "cu") -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = ANY(%s)",
            [table, list(types)],
        )
        return {row[0] for row in cursor.fetchall()}


# --- CashbackPolicy / CashbackTier ---


def test_policy_factory_defaults_str_ordering_and_checks() -> None:
    first = CashbackPolicyFactory.create(created_at=at(5, 20, 10), effective_from=at(6, 1))
    second = CashbackPolicyFactory.create(created_at=at(8, 1, 10), effective_from=at(8, 15))
    assert (first.version, second.version) == (1, 2)
    assert first.author is None and first.basis == "before_purchase" and first.earn_on_cashback_paid is False
    assert str(first) == "Cashback policy v1 (before_purchase, from 2026-06-01)"
    assert list(CashbackPolicy.objects.all()) == [second, first]  # ordering -version
    assert [(t.from_amount, t.to_amount, t.percent) for t in first.tiers.all()] == [
        (0, 20_000_000, Decimal("1.00"))
    ]
    violates("loyalty_cashbackpolicy_version_key", CashbackPolicyFactory.create, version=1, tiers=[])
    violates(
        "policy_effective_after_created",
        CashbackPolicyFactory.create,
        created_at=at(9, 1),
        effective_from=at(9, 1),
        tiers=[],
    )
    violates(
        "policy_effective_after_created",
        CashbackPolicyFactory.create,
        created_at=at(9, 1),
        effective_from=at(8, 31),
        tiers=[],
    )


def test_policy_author_protect_and_append_only_rules() -> None:
    author = StaffUserFactory.create()
    policy = CashbackPolicyFactory.create(author=author)
    with pytest.raises(ProtectedError):
        author.delete()
    with pytest.raises(AppendOnlyError):
        policy.delete()
    with pytest.raises(AppendOnlyError):
        CashbackPolicy.objects.filter(pk=policy.pk).delete()
    with pytest.raises(AppendOnlyError):
        CashbackTier.objects.filter(policy=policy).delete()
    assert StaffUser.objects.filter(pk=author.pk).exists()


def test_tier_constraints_str_and_ordering() -> None:
    policy = CashbackPolicyFactory.create(tiers=[(0, 20_000_000, "1.00"), (30_000_000, None, "3.00")])
    tiers = list(policy.tiers.all())
    assert [(t.from_amount, t.to_amount) for t in tiers] == [(0, 20_000_000), (30_000_000, None)]
    assert str(tiers[0]) == f"v{policy.pk} [0, 20000000) 1.00%"
    assert str(tiers[1]) == f"v{policy.pk} [30000000, ∞) 3.00%"
    violates("tier_policy_from_unique", CashbackTierFactory.create, policy=policy, from_amount=0)
    violates("tier_from_nonneg", CashbackTierFactory.create, policy=policy, from_amount=-1)
    violates("tier_to_above_from", CashbackTierFactory.create, policy=policy, from_amount=5, to_amount=5)
    violates("tier_to_above_from", CashbackTierFactory.create, policy=policy, from_amount=6, to_amount=5)
    violates(
        "tier_percent_range", CashbackTierFactory.create, policy=policy, from_amount=7, percent=Decimal("0")
    )
    violates(
        "tier_percent_range",
        CashbackTierFactory.create,
        policy=policy,
        from_amount=8,
        percent=Decimal("100.01"),
    )
    CashbackTierFactory.create(
        policy=policy, from_amount=50_000_000, to_amount=None, percent=Decimal("100.00")
    )  # upper bound ok
    # the ordering ["policy_id", "from_amount"] interleaves nothing across versions
    other = CashbackPolicyFactory.create(tiers=[(0, None, "0.57")])
    assert [t.policy_id for t in CashbackTier.objects.all()] == [policy.pk] * 3 + [other.pk]


def test_policy_derived_state_follows_the_seed_timeline() -> None:
    v1 = CashbackPolicyFactory.create(created_at=at(5, 20, 10), effective_from=at(6, 1))
    v2 = CashbackPolicyFactory.create(created_at=at(8, 1, 10), effective_from=at(8, 15))
    v3 = CashbackPolicyFactory.create(created_at=at(9, 20, 10), effective_from=at(10, 1))
    anchor = at(10, 24, 12, 5)
    rows = {p.version: p for p in CashbackPolicy.objects.with_derived()}
    assert [p.superseded_by for p in rows.values()] == [None, None, None]
    assert (rows[1].effective_to, rows[2].effective_to, rows[3].effective_to) == (at(8, 15), at(10, 1), None)
    assert [rows[v].state_at(anchor) for v in (1, 2, 3)] == ["expired", "expired", "active"]
    assert [rows[v].state_at(at(7, 1)) for v in (1, 2, 3)] == ["active", "scheduled", "scheduled"]
    assert [rows[v].state_at(at(5, 25)) for v in (1, 2, 3)] == ["scheduled", "scheduled", "scheduled"]
    assert [rows[v].state_at(at(9, 1)) for v in (1, 2, 3)] == ["expired", "active", "scheduled"]
    # "version active at T" for the engine
    assert CashbackPolicy.objects.active_at(at(5, 31, 23, 59)) is None
    assert CashbackPolicy.objects.active_at(at(6, 1, 9, 30)) == v1
    assert CashbackPolicy.objects.active_at(at(9, 14, 12, 20)) == v2
    assert CashbackPolicy.objects.active_at(anchor) == v3
    # a plain instance loads both derived columns lazily (one query) and ``state`` reads timeutils.now()
    plain = CashbackPolicy.objects.get(version=2)
    assert "_effective_to" not in plain.__dict__
    assert plain.effective_to == at(10, 1) and plain.superseded_by is None
    with pinned_clock(at(9, 1)):
        assert plain.state == "active"
    with pinned_clock(anchor):
        assert plain.state == "expired" and v3.state == "active" and v1.state == "expired"


def test_policy_refresh_from_db_re_derives_the_cached_columns() -> None:
    """The derived columns depend on later rows: a refresh must not keep values read before they existed."""
    v1 = CashbackPolicyFactory.create(created_at=at(1, 10), effective_from=at(2, 1))
    assert (v1.superseded_by, v1.effective_to) == (None, None)  # cached in __dict__
    # created while v1 is still scheduled, so it supersedes v1
    v2 = CashbackPolicyFactory.create(created_at=at(1, 20), effective_from=at(3, 1))
    with pinned_clock(at(1, 25)):
        assert v1.state == "scheduled"  # stale: still the pre-v2 picture
        v1.refresh_from_db()
        assert "_effective_to" not in v1.__dict__ and "_superseded_by" not in v1.__dict__
        assert (v1.superseded_by, v1.effective_to) == (v2.version, v2.effective_from)
        assert v1.state == "superseded"
    # a partial refresh and a refresh through a queryset drop the cache too
    assert v2.effective_to is None  # cached before v3 exists
    v3 = CashbackPolicyFactory.create(created_at=at(4, 1), effective_from=at(5, 1))
    assert v2.effective_to is None  # stale until refreshed
    v2.refresh_from_db(fields=["basis"])
    assert v2.effective_to == at(5, 1)
    assert v3.effective_to is None  # cached before v4 exists
    v4 = CashbackPolicyFactory.create(created_at=at(6, 1), effective_from=at(7, 1))
    v3.refresh_from_db(from_queryset=CashbackPolicy.objects.with_derived())
    assert v3.effective_to == at(7, 1) and v3.superseded_by is None
    assert v4.effective_to is None
    assert CashbackPolicy.objects.with_derived().get(pk=v1.pk).superseded_by == v1.superseded_by == 2


def test_policy_supersession_rule_t4() -> None:
    """``v`` is superseded by the smallest ``w > v`` with ``w.created_at < v.effective_from`` (L-2)."""
    v1 = CashbackPolicyFactory.create(created_at=at(1, 10), effective_from=at(2, 1))
    v2 = CashbackPolicyFactory.create(created_at=at(3, 1), effective_from=at(5, 1))  # scheduled far ahead
    v3 = CashbackPolicyFactory.create(
        created_at=at(3, 15), effective_from=at(4, 15)
    )  # created while v2 scheduled
    rows = {p.version: p for p in CashbackPolicy.objects.with_derived()}
    assert (rows[1].superseded_by, rows[2].superseded_by, rows[3].superseded_by) == (None, 3, None)
    assert (rows[1].effective_to, rows[2].effective_to, rows[3].effective_to) == (at(4, 15), at(4, 15), None)
    assert [rows[v].state_at(at(3, 20)) for v in (1, 2, 3)] == ["active", "superseded", "scheduled"]
    assert [rows[v].state_at(at(4, 20)) for v in (1, 2, 3)] == ["expired", "superseded", "active"]
    assert [rows[v].state_at(at(5, 2)) for v in (1, 2, 3)] == ["expired", "superseded", "active"]
    # v1 stays in force until the superseder starts; a superseded version is never the active one
    assert CashbackPolicy.objects.active_at(at(3, 20)) == v1
    assert CashbackPolicy.objects.active_at(at(4, 20)) == v3
    assert CashbackPolicy.objects.active_at(at(5, 2)) == v3
    assert v2.state_at(at(5, 2)) == "superseded"
    # a later version created after v's effective_from never supersedes it (it merely ends it)
    v4 = CashbackPolicyFactory.create(created_at=at(6, 2), effective_from=at(7, 1))
    rows = {p.version: p for p in CashbackPolicy.objects.with_derived()}
    assert rows[3].superseded_by is None and rows[3].effective_to == at(7, 1)
    assert rows[4].superseded_by is None and rows[4].effective_to is None
    assert CashbackPolicy.objects.active_at(at(7, 1)) == v4
    # at most one active version at any instant, and at most one scheduled once every row exists (02 §1.1;
    # a frozen table evaluated at an instant before a later creation naturally shows several "scheduled")
    for instant in (at(1, 15), at(2, 15), at(3, 20), at(4, 20), at(6, 15), at(8, 1)):
        states = [p.state_at(instant) for p in CashbackPolicy.objects.with_derived()]
        assert states.count("active") <= 1, (instant, states)
        if instant >= v4.created_at:
            assert states.count("scheduled") <= 1, (instant, states)


# --- LoyaltyAccount ---


def test_account_one_per_customer_str_negative_allowed_and_protect() -> None:
    account = LoyaltyAccountFactory.create(balance=-5_000)  # L5: negative balances are allowed
    assert str(account) == f"Loyalty account of customer {account.customer_id}: -5000"
    assert Customer.objects.get(pk=account.customer_id).loyalty_account == account
    violates(
        "loyalty_loyaltyaccount_customer_id_key", LoyaltyAccount.objects.create, customer=account.customer
    )
    with pytest.raises(ProtectedError):
        account.customer.delete()
    same = LoyaltyAccountFactory.create(customer=account.customer)  # get_or_create by customer
    assert same == account
    before = account.updated_at
    account.balance = 10
    account.save()
    assert account.updated_at >= before


# --- LedgerEntry ---


def test_ledger_factory_chain_str_ordering_and_fk_rules() -> None:
    account = LoyaltyAccountFactory.create()
    credit = LedgerEntryFactory.create(account=account, at=at(10, 21, 11, 47), amount=2_310, base=231_000)
    debit = LedgerEntryFactory.create(account=account, debit=True, amount=-2_000, at=at(10, 23, 14, 5))
    adjustment = LedgerEntryFactory.create(account=account, adjustment=True, at=at(10, 23, 14, 5))
    reversal = reverse_entry(credit, at=at(10, 23, 16, 10))
    account.refresh_from_db()
    assert [e.balance_after for e in (credit, debit, adjustment, reversal)] == [2_310, 310, 3_260, 950]
    assert account.balance == 950
    assert str(credit) == f"Ledger #{credit.pk} credit +2310 → 2310 (acct {account.pk})"
    assert str(debit) == f"Ledger #{debit.pk} debit -2000 → 310 (acct {account.pk})"
    # ordering -at, -id: ties on ``at`` fall back to id
    assert list(LedgerEntry.objects.all()) == [reversal, adjustment, debit, credit]
    assert list(account.entries.all()) == [reversal, adjustment, debit, credit]
    assert reversal.reverses == credit and credit.reversed_by == reversal
    assert reversal.source_type == credit.source_type and reversal.order == credit.order
    assert credit.policy is not None and credit.policy.ledger_entries.get() == credit
    assert debit.order is not None and set(debit.order.ledger_entries.all()) >= {debit}
    assert adjustment.actor is not None and adjustment.actor.ledger_adjustments.get() == adjustment
    # PROTECT: account, order, policy, actor and the reversed entry all refuse deletion
    with pytest.raises(ProtectedError):
        account.delete()
    with pytest.raises(ProtectedError):
        Order._default_manager.filter(pk=credit.order.pk).delete()  # type: ignore[union-attr]
    with pytest.raises(ProtectedError):
        adjustment.actor.delete()
    with pytest.raises(AppendOnlyError):
        credit.delete()
    with pytest.raises(AppendOnlyError):
        LedgerEntry.objects.filter(pk=reversal.pk).delete()


def test_ledger_amount_sign_and_reversal_link() -> None:
    account = LoyaltyAccountFactory.create()
    credit = LedgerEntryFactory.create(account=account)
    violates("ledger_amount_sign", LedgerEntryFactory.create, account=account, amount=0)
    violates("ledger_amount_sign", LedgerEntryFactory.create, account=account, amount=-1)
    violates("ledger_amount_sign", LedgerEntryFactory.create, account=account, debit=True, amount=1)
    violates("ledger_amount_sign", LedgerEntryFactory.create, account=account, debit=True, amount=0)
    violates("ledger_amount_sign", LedgerEntryFactory.create, account=account, adjustment=True, amount=0)
    violates(
        "ledger_amount_sign",
        LedgerEntryFactory.create,
        account=account,
        debit=True,
        kind="reversal",
        amount=0,
        reverses=credit,
    )
    violates(
        "ledger_reversal_link",
        LedgerEntryFactory.create,
        account=account,
        debit=True,
        kind="reversal",
        amount=-1_850,
    )
    violates("ledger_reversal_link", LedgerEntryFactory.create, account=account, reverses=credit)
    reversal = reverse_entry(credit)
    assert reversal.amount == -1_850
    violates(
        "loyalty_ledgerentry_reverses_id_key", reverse_entry, entry=credit
    )  # double cancel is impossible
    LedgerEntryFactory.create(
        account=account, adjustment=True, amount=-2_950
    )  # subtract adjustments are fine


def test_ledger_credit_fields_and_percent_range() -> None:
    account = LoyaltyAccountFactory.create()
    policy = CashbackPolicyFactory.create()
    for missing in ({"base": None}, {"percent": None}, {"policy": None}, {"base": -1}):
        violates(
            "ledger_credit_fields",
            LedgerEntryFactory.create,
            **{"account": account, "policy": policy, **missing},
        )
    for extra in ({"base": 100}, {"percent": Decimal("1.00")}, {"policy": policy}, {"promo_excluded": True}):
        violates("ledger_credit_fields", LedgerEntryFactory.create, account=account, debit=True, **extra)
    violates("ledger_percent_range", LedgerEntryFactory.create, account=account, percent=Decimal("0"))
    violates("ledger_percent_range", LedgerEntryFactory.create, account=account, percent=Decimal("100.5"))
    entry = LedgerEntryFactory.create(account=account, base=0, percent=Decimal("100.00"), promo_excluded=True)
    assert entry.base == 0 and entry.promo_excluded is True


def test_ledger_source_refs_admin_and_adjustment_fields() -> None:
    account = LoyaltyAccountFactory.create()
    order = OrderFactory.create(customer=account.customer, paid=True)
    purchase = PurchaseFactory.create(customer=account.customer, store=True)
    staff = StaffUserFactory.create()
    # ledger_admin_is_adjustment
    violates("ledger_admin_is_adjustment", LedgerEntryFactory.create, account=account, source_type="admin")
    violates(
        "ledger_admin_is_adjustment",
        LedgerEntryFactory.create,
        account=account,
        adjustment=True,
        source_type="order",
        order=order,
        source_ref=str(order.pk),
    )
    # ledger_source_refs
    violates("ledger_source_refs", LedgerEntryFactory.create, account=account, order=None, source_ref="1")
    violates("ledger_source_refs", LedgerEntryFactory.create, account=account, order=order, source_ref="")
    violates(
        "ledger_source_refs",
        LedgerEntryFactory.create,
        account=account,
        source_type="billz_sale",
        order=order,
        purchase=purchase,
        source_ref="S-1",
    )
    violates(
        "ledger_source_refs",
        LedgerEntryFactory.create,
        account=account,
        source_type="billz_sale",
        order=None,
        purchase=None,
        source_ref="S-1",
    )
    violates(
        "ledger_source_refs",
        LedgerEntryFactory.create,
        account=account,
        source_type="billz_sale",
        order=None,
        purchase=purchase,
        source_ref="",
    )
    violates(
        "ledger_source_refs", LedgerEntryFactory.create, account=account, adjustment=True, source_ref="x"
    )
    violates("ledger_source_refs", LedgerEntryFactory.create, account=account, adjustment=True, order=order)
    violates(
        "ledger_source_refs", LedgerEntryFactory.create, account=account, adjustment=True, purchase=purchase
    )
    # ledger_adjustment_fields
    violates(
        "ledger_adjustment_fields", LedgerEntryFactory.create, account=account, adjustment=True, actor=None
    )
    violates(
        "ledger_adjustment_fields", LedgerEntryFactory.create, account=account, adjustment=True, reason=""
    )
    violates("ledger_adjustment_fields", LedgerEntryFactory.create, account=account, reason="why")
    violates("ledger_adjustment_fields", LedgerEntryFactory.create, account=account, actor=staff)
    violates("ledger_adjustment_fields", LedgerEntryFactory.create, account=account, debit=True, actor=staff)
    # the valid shapes of the §1.4 table
    store = LedgerEntryFactory.create(account=account, store=True, purchase=purchase)
    assert (store.source_type, store.order, store.source_ref) == ("billz_sale", None, purchase.billz_sale_ref)
    online = LedgerEntryFactory.create(account=account, order=order, purchase=PurchaseFactory.create(order=order, customer=account.customer))  # fmt: skip  # noqa: E501
    assert online.source_ref == str(order.pk)
    adjustment = LedgerEntryFactory.create(account=account, adjustment=True, actor=staff)
    assert (adjustment.order, adjustment.purchase, adjustment.source_ref) == (None, None, "")


# --- CashbackHold ---


def test_hold_factory_traits_str_and_fk_rules() -> None:
    hold = CashbackHoldFactory.create()
    assert hold.state == "active" and hold.amount == 20_000 == hold.order.cashback_used
    assert hold.expires_at == hold.order.reservation_expires_at
    assert str(hold) == f"Hold 20000 on order {hold.order_id} (active)"
    assert hold.account.holds.get() == hold and hold.order.cashback_holds.get() == hold
    captured = CashbackHoldFactory.create(captured=True)
    assert captured.captured_entry is not None and captured.captured_entry.kind == "debit"
    assert captured.captured_entry.amount == -captured.amount
    assert (
        captured.captured_entry.order == captured.order and captured.captured_entry.at == captured.captured_at
    )
    assert captured.captured_entry.captured_hold == captured
    captured.account.refresh_from_db()
    assert captured.account.balance == 100_000 - 20_000
    released = CashbackHoldFactory.create(released=True)
    assert released.released_at == released.expires_at and released.release_reason == "reservation_expired"
    with pytest.raises(ProtectedError):
        hold.account.delete()
    with pytest.raises(ProtectedError):
        Order._default_manager.filter(pk=hold.order.pk).delete()
    with pytest.raises(AppendOnlyError):
        captured.captured_entry.delete()
    assert hold.updated_at >= hold.created_at


def test_hold_constraints() -> None:
    hold = CashbackHoldFactory.create()
    violates("hold_one_active_per_order", CashbackHoldFactory.create, account=hold.account, order=hold.order)
    # a released hold next to the active one is allowed (partial unique)
    CashbackHoldFactory.create(account=hold.account, order=hold.order, released=True)
    violates("hold_amount_positive", CashbackHoldFactory.create, amount=0)
    violates("hold_amount_positive", CashbackHoldFactory.create, amount=-1)
    now = hold.expires_at
    violates("hold_state_fields", CashbackHoldFactory.create, captured_at=now)
    violates("hold_state_fields", CashbackHoldFactory.create, released_at=now)
    violates("hold_state_fields", CashbackHoldFactory.create, release_reason="order_cancelled")
    violates("hold_state_fields", CashbackHoldFactory.create, state="captured")  # no entry, no captured_at
    violates("hold_state_fields", CashbackHoldFactory.create, state="captured", captured_at=now)
    violates(
        "hold_state_fields",
        CashbackHoldFactory.create,
        captured=True,
        captured_at=None,
        captured_entry__at=now,
    )
    violates("hold_state_fields", CashbackHoldFactory.create, captured=True, released_at=now)
    violates("hold_state_fields", CashbackHoldFactory.create, captured=True, release_reason="payment_failed")
    violates("hold_state_fields", CashbackHoldFactory.create, state="released")  # no released_at, no reason
    violates("hold_state_fields", CashbackHoldFactory.create, released=True, release_reason="")
    violates("hold_state_fields", CashbackHoldFactory.create, released=True, released_at=None)
    violates("hold_state_fields", CashbackHoldFactory.create, released=True, captured_at=now)
    with pytest.raises(IntegrityError, match="hold_state_fields"), transaction.atomic():
        entry = LedgerEntryFactory.create(debit=True)
        CashbackHoldFactory.create(
            account=entry.account, order=entry.order, amount=20_000, released=True, captured_entry=entry
        )


# --- Purchase / PurchaseItem ---


def test_purchase_factory_str_ordering_credit_and_reversal_rows() -> None:
    purchase = PurchaseFactory.create(at=at(10, 23, 14, 5))
    assert purchase.source == "online" and purchase.order is not None and purchase.order.purchase == purchase
    assert (purchase.amount, purchase.promo_amount, purchase.cashback_paid) == (185_000, 0, 0)
    assert purchase.cashback_amount == 0 and purchase.credited_entry is None and purchase.reversed is False
    assert str(purchase) == f"Purchase #{purchase.pk} online 185000 (order {purchase.order_id})"
    assert [(i.position, i.line_total, i.promo) for i in purchase.items.all()] == [(0, 185_000, False)]
    store = PurchaseFactory.create(
        customer=purchase.customer, store=True, at=at(10, 21, 11, 47), credit=2_310
    )
    assert (
        store.order is None and store.billz_order_uuid is None and store.billz_sale_ref == store.billz_sale_id
    )
    assert str(store) == f"Purchase #{store.pk} store_qr 231000 (Billz {store.billz_sale_ref})"
    assert store.credited_entry is not None and store.credited_entry.purchase == store
    assert (store.credited_entry.source_type, store.credited_entry.source_ref) == ("billz_sale", store.billz_sale_ref)  # fmt: skip  # noqa: E501
    assert store.credited_entry.credited_purchase == store and store.cashback_amount == 2_310
    assert list(Purchase.objects.all()) == [purchase, store]  # ordering -at, -id
    assert list(purchase.customer.purchases.all()) == [purchase, store]
    credited = PurchaseFactory.create(order__promo_subtotal=40_000, credit=1_450)
    assert credited.promo_amount == 40_000 and credited.credited_entry is not None
    assert credited.credited_entry.promo_excluded is True and credited.credited_entry.base == 145_000
    assert [(i.line_total, i.promo) for i in credited.items.all()] == [(145_000, False), (40_000, True)]
    reversal = reverse_purchase_rows(credited, at=at(10, 23, 16, 10))
    assert reversal is not None and reversal.reverses == credited.credited_entry
    assert (
        credited.reversed
        and credited.reversed_at == at(10, 23, 16, 10)
        and credited.reversal_entry == reversal
    )
    assert reversal.reversed_purchase == credited
    assert str(credited).endswith(" reversed")
    # Re-read: mypy keeps the Literal[False] narrowing of ``purchase.reversed`` across the helper call.
    assert reverse_purchase_rows(purchase) is None and Purchase.objects.get(pk=purchase.pk).reversed is True
    with pytest.raises(ProtectedError):
        purchase.customer.delete()
    with pytest.raises(ProtectedError):
        Order._default_manager.filter(pk=purchase.order.pk).delete()
    assert credited.policy is not None
    with pytest.raises(AppendOnlyError):  # append-only refuses before PROTECT would (02 §0.1)
        credited.policy.delete()
    assert Purchase._meta.get_field("policy").remote_field.on_delete is models.PROTECT


def test_purchase_unique_constraints() -> None:
    first = PurchaseFactory.create(store=True, billz_sale_id="S-88213")
    violates("purchase_billz_sale_unique", PurchaseFactory.create, store=True, billz_sale_id="S-88213")
    PurchaseFactory.create()
    PurchaseFactory.create()  # two online purchases with billz_sale_id "" are fine (partial unique)
    uuid = uuid4()
    PurchaseFactory.create(billz_order_uuid=uuid)
    violates("purchase_billz_uuid_unique", PurchaseFactory.create, billz_order_uuid=uuid)
    PurchaseFactory.create(store=True)
    PurchaseFactory.create(store=True)  # NULL uuids are distinct
    online = PurchaseFactory.create()
    violates(
        "loyalty_purchase_order_id_key", PurchaseFactory.create, order=online.order, customer=online.customer
    )
    assert Purchase.objects.filter(billz_sale_id=first.billz_sale_id).count() == 1


def test_purchase_check_constraints() -> None:
    for bad in (
        {"amount": -1},
        {"promo_amount": -1},
        {"cashback_paid": -1},
        {"cashback_amount": -1},
        {"lifetime_before": -1},
        {"promo_amount": 185_001},
        {"cashback_paid": 185_001},
    ):
        violates("purchase_amounts", PurchaseFactory.create, items=[], **bad)
    violates("purchase_source_refs", PurchaseFactory.create, order=None)
    violates("purchase_source_refs", PurchaseFactory.create, billz_order_uuid=None)
    violates("purchase_source_refs", PurchaseFactory.create, store=True, billz_sale_id="")
    violates("purchase_source_refs", PurchaseFactory.create, store=True, billz_order_uuid=uuid4())
    with pytest.raises(IntegrityError, match="purchase_source_refs"), transaction.atomic():
        order = OrderFactory.create(paid=True)
        PurchaseFactory.create(store=True, order=order, customer=order.customer)
    now = at(10, 24)
    violates("purchase_reversed_fields", PurchaseFactory.create, reversed=True)
    violates("purchase_reversed_fields", PurchaseFactory.create, reversed_at=now)
    credited = PurchaseFactory.create(credit=1_850)
    assert credited.credited_entry is not None
    reversal = reverse_entry(credited.credited_entry)
    with pytest.raises(IntegrityError, match="purchase_reversed_fields"), transaction.atomic():
        Purchase.objects.filter(pk=credited.pk).update(reversal_entry=reversal)  # reversed stays False
    violates("purchase_credit_link", PurchaseFactory.create, cashback_amount=5_680)
    with pytest.raises(IntegrityError, match="purchase_credit_link"), transaction.atomic():
        entry = LedgerEntryFactory.create()
        PurchaseFactory.create(customer=entry.account.customer, credited_entry=entry)
    with pytest.raises(IntegrityError, match="purchase_credit_link"), transaction.atomic():
        entry = LedgerEntryFactory.create()
        reversal = reverse_entry(entry)
        PurchaseFactory.create(
            customer=entry.account.customer, reversed=True, reversed_at=now, reversal_entry=reversal
        )
    # the design's write order: insert with 0 / NULL, post the entry, then one UPDATE of both columns
    purchase = PurchaseFactory.create(credit=1_850)
    assert purchase.cashback_amount == 1_850 and purchase.credited_entry is not None
    with pytest.raises(IntegrityError, match="purchase_credit_link"), transaction.atomic():
        Purchase.objects.filter(pk=purchase.pk).update(credited_entry=None)
    with pytest.raises(IntegrityError, match="purchase_credit_link"), transaction.atomic():
        Purchase.objects.filter(pk=purchase.pk).update(cashback_amount=0)


def test_purchase_item_constraints_cascade_and_protect() -> None:
    purchase = PurchaseFactory.create(items=[])
    product = ProductFactory.create(price=45_000)
    item = PurchaseItemFactory.create(purchase=purchase, product=product, quantity=2)
    assert (item.position, item.unit_price, item.line_total, item.billz_product_id) == (0, 45_000, 90_000, product.billz_id)  # fmt: skip  # noqa: E501
    assert str(item) == f"{product.display_name} × 2 @ 45000 (purchase {purchase.pk})"
    billz_only = PurchaseItemFactory.create(
        purchase=purchase, product=None, name="Billz line", unit_price=1_000
    )
    assert billz_only.position == 1 and billz_only.billz_product_id == ""
    assert list(purchase.items.all()) == [item, billz_only]
    violates("purchaseitem_position_unique", PurchaseItemFactory.create, purchase=purchase, position=0)
    violates("purchaseitem_amounts", PurchaseItemFactory.create, purchase=purchase, quantity=0)
    violates("purchaseitem_amounts", PurchaseItemFactory.create, purchase=purchase, unit_price=-1)
    violates(
        "purchaseitem_amounts", PurchaseItemFactory.create, purchase=purchase, unit_price=10, line_total=-1
    )
    with pytest.raises(ProtectedError):
        product.delete()
    purchase_pk = purchase.pk
    purchase.delete()  # CASCADE: the only cascade of the module (02 §0.1)
    assert not PurchaseItem.objects.filter(purchase_id=purchase_pk).exists()


# --- PosDevice ---


def test_pos_device_unique_key_str_ordering_and_protect() -> None:
    staff = StaffUserFactory.create()
    device = PosDeviceFactory.create(name="Yunusobod kassa", created_by=staff)
    assert str(device) == f"POS Yunusobod kassa ({device.key_prefix}…)"
    inactive = PosDeviceFactory.create(name="Chilonzor kassa", is_active=False)
    assert str(inactive).endswith(" inactive")
    assert list(PosDevice.objects.all()) == [inactive, device]  # ordering name
    violates("loyalty_posdevice_key_hash_key", PosDeviceFactory.create, key_hash=device.key_hash)
    with pytest.raises(ProtectedError):
        staff.delete()
    assert staff.pos_devices.get() == device


# --- pg_catalog ---


def test_indexes_and_constraints_as_postgres_stores_them() -> None:
    policy_idx = index_definitions("loyalty_cashbackpolicy")
    assert {"policy_effective_from_idx", "loyalty_cashbackpolicy_version_key"} <= set(policy_idx)
    assert constraint_names("loyalty_cashbackpolicy", "c") == {
        "policy_effective_after_created",
        "loyalty_cashbackpolicy_version_check",  # PositiveIntegerField: version >= 0
    }
    assert constraint_names("loyalty_cashbacktier", "c") == {
        "tier_from_nonneg",
        "tier_to_above_from",
        "tier_percent_range",
    }
    assert "tier_policy_from_unique" in constraint_names("loyalty_cashbacktier", "u")
    assert "loyalty_loyaltyaccount_customer_id_key" in constraint_names("loyalty_loyaltyaccount", "u")

    ledger_idx = index_definitions("loyalty_ledgerentry")
    assert ledger_idx["ledger_account_at_idx"].endswith("USING btree (account_id, at, id)")
    assert ledger_idx["ledger_order_idx"].endswith("USING btree (order_id)")
    assert ledger_idx["ledger_purchase_idx"].endswith("USING btree (purchase_id)")
    assert "loyalty_ledgerentry_reverses_id_key" in constraint_names("loyalty_ledgerentry", "u")
    assert constraint_names("loyalty_ledgerentry", "c") == {
        "ledger_amount_sign",
        "ledger_reversal_link",
        "ledger_credit_fields",
        "ledger_admin_is_adjustment",
        "ledger_source_refs",
        "ledger_adjustment_fields",
        "ledger_percent_range",
    }

    hold_idx = index_definitions("loyalty_cashbackhold")
    assert hold_idx["hold_one_active_per_order"].startswith("CREATE UNIQUE INDEX")
    assert hold_idx["hold_one_active_per_order"].endswith("(order_id) WHERE ((state)::text = 'active'::text)")
    assert hold_idx["hold_active_account_idx"].endswith("(account_id) WHERE ((state)::text = 'active'::text)")
    assert hold_idx["hold_active_expires_idx"].endswith("(expires_at) WHERE ((state)::text = 'active'::text)")
    assert constraint_names("loyalty_cashbackhold", "c") == {"hold_amount_positive", "hold_state_fields"}

    purchase_idx = index_definitions("loyalty_purchase")
    assert purchase_idx["purchase_customer_at_idx"].endswith("USING btree (customer_id, at, id)")
    assert purchase_idx["purchase_live_cust_src_idx"].endswith("(customer_id, source) WHERE (NOT reversed)")
    assert purchase_idx["purchase_billz_sale_unique"].startswith("CREATE UNIQUE INDEX")
    assert "WHERE (NOT ((billz_sale_id)::text = ''::text))" in purchase_idx["purchase_billz_sale_unique"]
    assert {"purchase_billz_uuid_unique", "loyalty_purchase_order_id_key"} <= constraint_names(
        "loyalty_purchase", "u"
    )
    assert constraint_names("loyalty_purchase", "c") == {
        "purchase_amounts",
        "purchase_source_refs",
        "purchase_reversed_fields",
        "purchase_credit_link",
    }
    assert "purchaseitem_position_unique" in constraint_names("loyalty_purchaseitem", "u")
    assert constraint_names("loyalty_purchaseitem", "c") == {
        "purchaseitem_amounts",
        "loyalty_purchaseitem_position_check",  # PositiveSmallIntegerField / PositiveIntegerField: >= 0
        "loyalty_purchaseitem_quantity_check",
    }
    assert "loyalty_posdevice_key_hash_key" in constraint_names("loyalty_posdevice", "u")
    for name in (
        *policy_idx,
        *ledger_idx,
        *hold_idx,
        *purchase_idx,
        *constraint_names("loyalty_ledgerentry"),
        *constraint_names("loyalty_purchase"),
    ):
        assert len(name) <= 63, name  # PostgreSQL identifier limit (02 §0.1)


# --- Reconciliation checks (02 §1.9.3) over factory-built chains ---

RECONCILIATION_SQL = {
    "C1": """
        SELECT a.id FROM loyalty_loyaltyaccount a
        LEFT JOIN loyalty_ledgerentry e ON e.account_id = a.id
        GROUP BY a.id, a.balance HAVING a.balance <> COALESCE(SUM(e.amount), 0)""",
    "C2": """
        SELECT a.id FROM loyalty_loyaltyaccount a
        JOIN LATERAL (SELECT e.balance_after FROM loyalty_ledgerentry e
                      WHERE e.account_id = a.id ORDER BY e.at DESC, e.id DESC LIMIT 1) last ON true
        WHERE last.balance_after <> a.balance""",
    "C3": """
        SELECT id FROM (
          SELECT e.id, e.amount, e.balance_after,
                 LAG(e.balance_after, 1, 0) OVER (PARTITION BY e.account_id ORDER BY e.at, e.id) AS prev_after
          FROM loyalty_ledgerentry e) x
        WHERE balance_after <> prev_after + amount""",
    "C5": """
        SELECT p.id FROM loyalty_purchase p
        LEFT JOIN loyalty_ledgerentry c ON c.id = p.credited_entry_id
        LEFT JOIN loyalty_ledgerentry r ON r.id = p.reversal_entry_id
        WHERE (p.cashback_amount > 0 AND (c.amount <> p.cashback_amount OR c.purchase_id <> p.id))
           OR (p.reversed AND p.credited_entry_id IS NOT NULL
               AND (r.id IS NULL OR r.reverses_id <> p.credited_entry_id))""",
    "C6": """
        SELECT h.id FROM loyalty_cashbackhold h JOIN loyalty_ledgerentry d ON d.id = h.captured_entry_id
        WHERE d.kind <> 'debit' OR d.amount <> -h.amount OR d.order_id <> h.order_id""",
}


def mismatches() -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    with connection.cursor() as cursor:
        for check, sql in RECONCILIATION_SQL.items():
            cursor.execute(sql)
            result[check] = [row[0] for row in cursor.fetchall()]
    return result


def test_factory_chains_pass_the_nightly_reconciliation_checks() -> None:
    account = LoyaltyAccountFactory.create()
    t0 = at(10, 1, 9)
    store = PurchaseFactory.create(customer=account.customer, store=True, at=t0, credit=2_310)
    LedgerEntryFactory.create(account=account, adjustment=True, at=t0 + timedelta(days=1))
    hold = CashbackHoldFactory.create(
        account=account, captured=True, order__created_at=t0 + timedelta(days=2)
    )
    online = PurchaseFactory.create(
        customer=account.customer, order=hold.order, at=t0 + timedelta(days=2, minutes=3), credit=5_680
    )
    reverse_purchase_rows(online, at=t0 + timedelta(days=3))
    reverse_entry(hold.captured_entry, at=t0 + timedelta(days=3))  # type: ignore[arg-type]
    account.refresh_from_db()
    assert account.balance == 2_310 + 2_950 - 20_000 + 5_680 - 5_680 + 20_000
    assert store.credited_entry is not None and online.reversal_entry is not None
    chain = list(
        LedgerEntry.objects.order_by("at", "id").values_list("id", "kind", "amount", "balance_after", "at")
    )
    assert mismatches() == {"C1": [], "C2": [], "C3": [], "C5": [], "C6": []}, chain
    # an entry whose balance_after was forced (and whose amount never reached the balance) is exactly what
    # C1/C2/C3 exist to catch
    broken = LedgerEntryFactory.create(account=account, balance_after=1, at=t0 + timedelta(days=4))
    found = mismatches()
    assert found["C1"] == [account.pk] and found["C2"] == [account.pk] and found["C3"] == [broken.pk]
