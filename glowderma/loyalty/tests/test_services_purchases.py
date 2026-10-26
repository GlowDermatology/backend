"""loyalty.services — online and store purchases, the reversal flow and the Billz link (design 02 §1.6, §1.10,
§1.9.1 vectors through the database)."""

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core.models import AuditEntry
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import selectors, services, tasks
from glowderma.loyalty.models import LedgerEntry, LoyaltyAccount, Purchase, PurchaseItem
from glowderma.loyalty.services import PurchaseLine
from glowderma.loyalty.tests.factories import CashbackPolicyFactory, reverse_entry
from glowderma.loyalty.tests.support import at, chain, paid_order, seed_policies

pytestmark = pytest.mark.django_db

ORDER_10248_LINES = (
    PurchaseLine(name="Ceramide krem", quantity=1, unit_price=312_000, line_total=312_000),
    PurchaseLine(
        name="Retinol serum", quantity=1, unit_price=256_000, line_total=256_000, billz_product_id="B-7"
    ),
    PurchaseLine(name="SPF 50", quantity=1, unit_price=240_000, line_total=240_000, promo=True),
)


def test_record_online_purchase_reproduces_order_10248() -> None:
    seed_policies()
    customer = CustomerFactory.create()
    # lifetime before №10248 = 12 772 000 (one store sale), balance 48 600 to spend
    services.record_store_purchase(customer.pk, "S-1", "S-1", 12_772_000, at(9, 5, 12), at=at(9, 5, 12))
    staff = StaffUserFactory.create()
    services.adjust(customer.pk, "subtract", 127_720 - 48_600, "balansni to‘g‘rilash", staff.pk, at=at(10, 1))
    order = paid_order(customer, created_at=at(10, 23, 14, 2), items_subtotal=808_000, promo_subtotal=240_000, cashback_used=48_600)  # fmt: skip  # noqa: E501
    services.create_hold(customer.pk, order.pk, 48_600, order.reservation_expires_at)
    paid_at = at(10, 23, 14, 5)
    services.capture_hold(order.pk, at=paid_at)
    design_uuid = UUID("8d1f1a62-6f1c-4d5b-9c3e-2a7b4e0f1248")
    result = services.record_online_purchase(
        customer.pk,
        order.pk,
        808_000,
        240_000,
        48_600,
        paid_at,
        items=ORDER_10248_LINES,
        billz_order_uuid=design_uuid,
    )
    assert result.created and result.policy_version == 3
    assert (result.base, result.percent, result.cashback_amount, result.promo_excluded) == (568_000, Decimal("1.00"), 5_680, True)  # fmt: skip  # noqa: E501
    assert (result.lifetime_before, result.billz_order_uuid, result.source, result.order_id) == (12_772_000, design_uuid, "online", order.pk)  # fmt: skip  # noqa: E501
    assert result.billz_sale_id == "" and result.at == paid_at and result.credited_entry_id is not None
    purchase = Purchase.objects.get(pk=result.purchase_id)
    assert (purchase.amount, purchase.promo_amount, purchase.cashback_paid, purchase.cashback_amount) == (808_000, 240_000, 48_600, 5_680)  # fmt: skip  # noqa: E501
    assert purchase.credited_entry_id == result.credited_entry_id and purchase.policy is not None
    assert purchase.policy.version == 3 and purchase.billz_pushed_at is None and not purchase.reversed
    entry = LedgerEntry.objects.get(pk=result.credited_entry_id)
    assert (entry.kind, entry.amount, entry.balance_after, entry.source_type, entry.order_id, entry.purchase_id, entry.source_ref) == ("credit", 5_680, 5_680, "order", order.pk, purchase.pk, str(order.pk))  # fmt: skip  # noqa: E501
    assert (entry.base, entry.percent, entry.promo_excluded, entry.policy_id, entry.at) == (568_000, Decimal("1.00"), True, purchase.policy_id, paid_at)  # fmt: skip  # noqa: E501
    assert [
        (i.position, i.name, i.unit_price, i.promo, i.billz_product_id) for i in purchase.items.all()
    ] == [
        (0, "Ceramide krem", 312_000, False, ""),
        (1, "Retinol serum", 256_000, False, "B-7"),
        (2, "SPF 50", 240_000, True, ""),
    ]
    assert selectors.account_summary(customer.pk) == selectors.AccountSummary(5_680, 0, 5_680)
    assert selectors.customer_totals(customer.pk) == selectors.Totals(12_772_000, 808_000, 13_580_000, 1)
    # a redelivery returns the existing purchase untouched
    replay = services.record_online_purchase(customer.pk, order.pk, 808_000, 240_000, 48_600, paid_at)
    assert not replay.created and replace(replay, created=True) == result
    assert (replay.purchase_id, replay.credited_entry_id, replay.cashback_amount, replay.base, replay.percent, replay.policy_version, replay.billz_order_uuid) == (purchase.pk, entry.pk, 5_680, 568_000, Decimal("1.00"), 3, design_uuid)  # fmt: skip  # noqa: E501
    assert Purchase.objects.count() == 2 and LedgerEntry.objects.count() == 4


def test_record_online_purchase_without_policy_or_covering_tier() -> None:
    customer = CustomerFactory.create()
    early = paid_order(customer, created_at=at(5, 10, 10), items_subtotal=312_000)
    before_any_policy = services.record_online_purchase(
        customer.pk, early.pk, 312_000, 0, 0, at(5, 10, 10, 3)
    )
    assert (before_any_policy.policy_version, before_any_policy.cashback_amount, before_any_policy.credited_entry_id, before_any_policy.percent) == (None, 0, None, None)  # fmt: skip  # noqa: E501
    assert before_any_policy.base == 312_000 and before_any_policy.lifetime_before == 0
    purchase = Purchase.objects.get(pk=before_any_policy.purchase_id)
    assert purchase.policy is None and purchase.cashback_amount == 0 and purchase.credited_entry is None
    assert not LoyaltyAccount.objects.get(customer=customer).entries.exists()
    # a policy whose tiers end at 50M: lifetime 50M is uncovered → policy recorded, nothing credited
    policy = CashbackPolicyFactory.create(
        created_at=at(9, 1), effective_from=at(9, 2), tiers=[(0, 50_000_000, "1.00")]
    )
    big = services.record_store_purchase(customer.pk, "S-BIG", "S-BIG", 49_688_000, at(9, 3))
    later = paid_order(customer, created_at=at(9, 4), items_subtotal=100_000)
    uncovered = services.record_online_purchase(customer.pk, later.pk, 100_000, 0, 0, at(9, 4, 0, 3))
    assert big.cashback_amount == 496_880 and uncovered.lifetime_before == 50_000_000
    assert (uncovered.policy_version, uncovered.percent, uncovered.cashback_amount, uncovered.credited_entry_id) == (policy.version, None, 0, None)  # fmt: skip  # noqa: E501
    assert Purchase.objects.get(pk=uncovered.purchase_id).policy_id == policy.pk
    assert not replay_changes(customer.pk)


def replay_changes(customer_id: int) -> bool:
    """A replay of every purchase of the customer must not add rows."""
    before = (Purchase.objects.count(), LedgerEntry.objects.count())
    for purchase in Purchase.objects.filter(customer_id=customer_id):
        if purchase.order_id is not None:
            services.record_online_purchase(
                customer_id, purchase.order_id, purchase.amount, 0, 0, purchase.at
            )
        else:
            services.record_store_purchase(
                customer_id, purchase.billz_sale_id, "", purchase.amount, purchase.at
            )
    return before != (Purchase.objects.count(), LedgerEntry.objects.count())


def test_record_online_purchase_with_explicit_policy_version_and_bad_amounts() -> None:
    policies = seed_policies()
    customer = CustomerFactory.create()
    order = paid_order(customer, created_at=at(10, 5), items_subtotal=100_000)
    with pytest.raises(ValueError, match="amounts"):
        services.record_online_purchase(customer.pk, order.pk, 100_000, 100_001, 0, at(10, 5, 0, 3))
    with pytest.raises(ValueError, match="amounts"):
        services.record_online_purchase(customer.pk, order.pk, 100_000, 0, 100_001, at(10, 5, 0, 3))
    assert Purchase.objects.count() == 0
    # v1 (earn=false) instead of the active v3 (earn=true): the cashback-paid part is excluded
    result = services.record_online_purchase(
        customer.pk, order.pk, 100_000, 0, 30_000, at(10, 5, 0, 3), policy_version=1
    )
    assert (result.policy_version, result.base, result.cashback_amount) == (1, 70_000, 700)
    assert Purchase.objects.get(pk=result.purchase_id).policy_id == policies[1].pk
    assert isinstance(result.billz_order_uuid, UUID)


def test_record_store_purchase_vectors_and_idempotency() -> None:
    seed_policies()
    customer = CustomerFactory.create()
    with pinned_clock(at(9, 5, 12)):
        first = services.record_store_purchase(customer.pk, "S-80091", "S-80091", 361_000, at(9, 5, 12))
    assert first.created and (first.policy_version, first.base, first.percent, first.cashback_amount) == (2, 361_000, Decimal("1.00"), 3_610)  # fmt: skip  # noqa: E501
    assert (first.source, first.order_id, first.billz_order_uuid, first.billz_sale_id, first.billz_sale_ref, first.cashback_paid) == ("store_qr", None, None, "S-80091", "S-80091", 0)  # fmt: skip  # noqa: E501
    assert first.credited_entry_id is not None
    entry = LedgerEntry.objects.get(pk=first.credited_entry_id)
    assert (entry.source_type, entry.source_ref, entry.order_id, entry.purchase_id, entry.at) == ("billz_sale", "S-80091", None, first.purchase_id, at(9, 5, 12))  # fmt: skip  # noqa: E501
    purchase = Purchase.objects.get(pk=first.purchase_id)
    assert (purchase.at, purchase.created_at, purchase.lifetime_before) == (at(9, 5, 12), at(9, 5, 12), 0)
    # the sale time picks the policy and is kept on the purchase; ``at`` is the posting time of the entry
    second = services.record_store_purchase(customer.pk, "S-88213", "", 231_000, at(10, 21, 11, 47), at=at(10, 21, 11, 50), items=[PurchaseLine(name="Chek", quantity=1, unit_price=231_000, line_total=231_000)])  # fmt: skip  # noqa: E501
    assert (second.policy_version, second.cashback_amount, second.billz_sale_ref, second.lifetime_before) == (3, 2_310, "S-88213", 361_000)  # fmt: skip  # noqa: E501
    assert second.credited_entry_id is not None
    assert LedgerEntry.objects.get(pk=second.credited_entry_id).at == at(10, 21, 11, 50)
    assert PurchaseItem.objects.filter(purchase_id=second.purchase_id).count() == 1
    assert chain(entry.account_id) == [("credit", 3_610, 3_610), ("credit", 2_310, 5_920)]
    replay = services.record_store_purchase(customer.pk, "S-80091", "other ref", 999, at(1, 1))
    assert not replay.created and replay.purchase_id == first.purchase_id and replay.amount == 361_000
    assert replace(replay, created=True) == first
    assert replay.cashback_amount == 3_610 and replay.base == 361_000 and replay.percent == Decimal("1.00")
    with pytest.raises(ValueError, match="sale id"):
        services.record_store_purchase(customer.pk, "", "", 1, at(1, 1))
    with pytest.raises(ValueError, match="amounts"):
        services.record_store_purchase(customer.pk, "S-NEG", "", -1, at(1, 1))
    assert Purchase.objects.count() == 2 and selectors.customer_totals(customer.pk).store == 592_000


def paid_order_with_cashback(
    customer: Any, *, items_subtotal: int, promo: int, cashback_used: int, when: tuple[int, int, int, int]) -> tuple[Any, services.PurchaseResult]:  # fmt: skip  # noqa: E501
    order = paid_order(customer, created_at=at(*when), items_subtotal=items_subtotal, promo_subtotal=promo, cashback_used=cashback_used)  # fmt: skip  # noqa: E501
    if cashback_used:
        services.create_hold(order.customer_id, order.pk, cashback_used, order.reservation_expires_at)
    services.capture_hold(order.pk, at=order.paid_at)
    result = services.record_online_purchase(order.customer_id, order.pk, items_subtotal, promo, cashback_used, order.paid_at)  # fmt: skip  # noqa: E501
    return order, result


def test_reverse_purchase_reverses_debit_and_credit_once() -> None:
    seed_policies()
    customer = CustomerFactory.create()
    staff = StaffUserFactory.create()
    services.record_store_purchase(
        customer.pk, "S-1", "S-1", 3_000_000, at(10, 1, 9), at=at(10, 1, 9)
    )  # +30 000
    order, result = paid_order_with_cashback(customer, items_subtotal=554_000, promo=240_000, cashback_used=20_000, when=(10, 18, 15, 22))  # fmt: skip  # noqa: E501
    assert result.cashback_amount == 3_140
    account_id = LoyaltyAccount.objects.get(customer=customer).pk
    assert chain(account_id) == [("credit", 30_000, 30_000), ("debit", -20_000, 10_000), ("credit", 3_140, 13_140)]  # fmt: skip  # noqa: E501
    assert selectors.customer_totals(customer.pk) == selectors.Totals(3_000_000, 554_000, 3_554_000, 1)
    reversed_at = at(10, 18, 20, 25)
    reversal = services.reverse_purchase(result.purchase_id, reversed_at, actor_id=staff.pk)
    assert not reversal.already_reversed and reversal.reversed_at == reversed_at
    assert len(reversal.reversal_entry_ids) == 2 and reversal.net_amount == 20_000 - 3_140
    purchase = Purchase.objects.get(pk=result.purchase_id)
    assert (purchase.reversed, purchase.reversed_at, purchase.reversal_entry_id) == (True, reversed_at, reversal.credit_reversal_entry_id)  # fmt: skip  # noqa: E501
    rows = list(LedgerEntry.objects.filter(pk__in=reversal.reversal_entry_ids).order_by("id"))
    debit = LedgerEntry.objects.get(order_id=order.pk, kind="debit")
    assert [
        (
            r.kind,
            r.amount,
            r.reverses_id,
            r.source_type,
            r.order_id,
            r.purchase_id,
            r.source_ref,
            r.at,
            r.actor_id,
        )
        for r in rows
    ] == [  # fmt: skip  # noqa: E501
        ("reversal", 20_000, debit.pk, "order", order.pk, None, str(order.pk), reversed_at, None),
        (
            "reversal",
            -3_140,
            result.credited_entry_id,
            "order",
            order.pk,
            result.purchase_id,
            str(order.pk),
            reversed_at,
            None,
        ),  # fmt: skip  # noqa: E501
    ]
    assert chain(account_id)[3:] == [("reversal", 20_000, 33_140), ("reversal", -3_140, 30_000)]
    # the lifetime drops by the purchase amount; the reversed purchase keeps its numbers for history
    assert selectors.customer_totals(customer.pk) == selectors.Totals(3_000_000, 0, 3_000_000, 0)
    assert purchase.amount == 554_000 and purchase.cashback_amount == 3_140
    # double reversal: refused as a no-op (unique reverses), nothing new posted
    again = services.reverse_purchase(result.purchase_id, at(10, 19))
    assert again.already_reversed and again.reversal_entry_ids == () and again.net_amount == 0
    assert again.reversed_at == reversed_at and again.credit_reversal_entry_id is None
    assert (
        LedgerEntry.objects.count() == 5 and Purchase.objects.get(pk=purchase.pk).reversed_at == reversed_at
    )
    assert not AuditEntry.objects.filter(action="loyalty.balance_negative").exists()


def test_reverse_purchase_partial_and_negative_balance() -> None:
    seed_policies()
    customer = CustomerFactory.create()
    staff = StaffUserFactory.create()
    services.record_store_purchase(
        customer.pk, "S-1", "S-1", 1_000_000, at(10, 1, 9), at=at(10, 1, 9)
    )  # +10 000
    order_a, a = paid_order_with_cashback(customer, items_subtotal=800_000, promo=0, cashback_used=0, when=(10, 10, 10, 0))  # +8 000 → 18 000  # fmt: skip  # noqa: E501
    order_b, b = paid_order_with_cashback(customer, items_subtotal=500_000, promo=0, cashback_used=18_000, when=(10, 14, 10, 0))  # −18 000, +5 000 → 5 000  # fmt: skip  # noqa: E501
    assert selectors.account_summary(customer.pk).balance == 5_000
    # the debit of B was already reversed by hand: only the credit is left to reverse
    debit_b = LedgerEntry.objects.get(order_id=order_b.pk, kind="debit")
    reverse_entry(debit_b, at=at(10, 15))  # +18 000 → 23 000
    partial = services.reverse_purchase(b.purchase_id, at(10, 15, 0, 1))
    assert len(partial.reversal_entry_ids) == 1 and partial.net_amount == -5_000
    assert partial.credit_reversal_entry_id == partial.reversal_entry_ids[0]
    assert Purchase.objects.get(pk=b.purchase_id).reversal_entry_id == partial.credit_reversal_entry_id
    # cancelling A after its cashback was spent drives the balance below zero (L5): flagged with the actor
    services.adjust(
        customer.pk, "subtract", 18_000, "sovg‘a kartasiga o‘tkazildi", staff.pk, at=at(10, 16)
    )  # 18 000 → 0
    negative = services.reverse_purchase(a.purchase_id, at(10, 16, 12), actor_id=staff.pk)
    assert negative.net_amount == -8_000 and selectors.account_summary(customer.pk) == selectors.AccountSummary(-8_000, 0, -8_000)  # fmt: skip  # noqa: E501
    flag = AuditEntry.objects.get(action="loyalty.balance_negative")
    assert flag.after is not None
    assert (flag.actor_type, flag.actor_id, flag.after["balance"], flag.after["kind"]) == ("staff", staff.pk, -8_000, "reversal")  # fmt: skip  # noqa: E501
    assert selectors.customer_totals(customer.pk) == selectors.Totals(1_000_000, 0, 1_000_000, 0)
    assert Purchase.objects.filter(reversed=True).count() == 2


def test_reverse_store_purchase_and_unknown_purchase() -> None:
    seed_policies()
    customer = CustomerFactory.create()
    sale = services.record_store_purchase(customer.pk, "S-7", "S-7", 412_000, at(10, 9, 13, 40), at=at(10, 9, 13, 40))  # fmt: skip  # noqa: E501
    uncredited = services.record_store_purchase(customer.pk, "S-8", "S-8", 50, at(10, 9, 13, 41), at=at(10, 9, 13, 41))  # 0.5 → 0  # fmt: skip  # noqa: E501
    assert uncredited.cashback_amount == 0 and uncredited.credited_entry_id is None
    reversal = services.reverse_purchase(sale.purchase_id, at(10, 10))
    assert len(reversal.reversal_entry_ids) == 1 and reversal.net_amount == -4_120
    row = LedgerEntry.objects.get(pk=reversal.reversal_entry_ids[0])
    assert (row.source_type, row.source_ref, row.purchase_id, row.order_id, row.reverses_id) == ("billz_sale", "S-7", sale.purchase_id, None, sale.credited_entry_id)  # fmt: skip  # noqa: E501
    none = services.reverse_purchase(uncredited.purchase_id, at(10, 10))
    assert none.reversal_entry_ids == () and Purchase.objects.get(pk=uncredited.purchase_id).reversed
    assert Purchase.objects.get(pk=uncredited.purchase_id).reversal_entry_id is None
    with pytest.raises(services.PurchaseNotFound):
        services.reverse_purchase(10**9, at(10, 10))
    assert selectors.customer_totals(customer.pk).lifetime == 0


def test_link_billz_sale() -> None:
    seed_policies()
    customer = CustomerFactory.create()
    order, result = paid_order_with_cashback(customer, items_subtotal=185_000, promo=0, cashback_used=0, when=(10, 20, 9, 0))  # fmt: skip  # noqa: E501
    assert result.billz_order_uuid is not None  # persisted with the purchase, before any push
    info = selectors.billz_push_info(result.purchase_id)
    assert info == selectors.BillzPushInfo(result.purchase_id, customer.pk, order.pk, result.billz_order_uuid, "", "", None)  # fmt: skip  # noqa: E501
    assert selectors.billz_push_info(10**9) is None
    # a payload of another purchase (wrong Billz order id) is refused before anything is written
    with pytest.raises(services.BillzSaleConflict, match="pushed as Billz order"):
        services.link_billz_sale(result.purchase_id, billz_order_uuid=uuid4())
    # the push succeeded without a sale id: only billz_pushed_at is set (the handler's skip guard)
    pushed = services.link_billz_sale(result.purchase_id, billz_order_uuid=result.billz_order_uuid, pushed_at=at(10, 20, 9, 1))  # fmt: skip  # noqa: E501
    assert pushed == services.LinkResult(result.purchase_id, result.billz_order_uuid, "", "", at(10, 20, 9, 1), True)  # fmt: skip  # noqa: E501
    again = services.link_billz_sale(result.purchase_id, billz_order_uuid=result.billz_order_uuid, pushed_at=at(10, 20, 9, 9))  # fmt: skip  # noqa: E501
    assert not again.changed and again.billz_pushed_at == at(10, 20, 9, 1)
    # the sale id discovered later (M3 sync echo) is stored; the push instant is kept
    linked = services.link_billz_sale(result.purchase_id, billz_sale_id="bz-123", billz_sale_ref="S-90001", pushed_at=at(10, 21))  # fmt: skip  # noqa: E501
    assert linked == services.LinkResult(result.purchase_id, result.billz_order_uuid, "bz-123", "S-90001", at(10, 20, 9, 1), True)  # fmt: skip  # noqa: E501
    purchase = Purchase.objects.get(pk=result.purchase_id)
    assert (purchase.billz_sale_id, purchase.billz_sale_ref, purchase.billz_pushed_at) == ("bz-123", "S-90001", at(10, 20, 9, 1))  # fmt: skip  # noqa: E501
    replay = services.link_billz_sale(result.purchase_id, billz_sale_id="bz-123")
    assert not replay.changed and replay.billz_sale_ref == "S-90001"
    assert selectors.billz_push_info(result.purchase_id).billz_pushed_at == at(10, 20, 9, 1)  # type: ignore[union-attr]
    with pytest.raises(services.BillzSaleConflict, match="already linked"):
        services.link_billz_sale(result.purchase_id, billz_sale_id="bz-999")
    # it never credits again, and the same Billz sale cannot be re-ingested as a store purchase
    assert LedgerEntry.objects.count() == 1
    store = services.record_store_purchase(customer.pk, "bz-123", "", 185_000, at(10, 20, 9, 5))
    assert not store.created and store.purchase_id == result.purchase_id
    sale = services.record_store_purchase(customer.pk, "S-55", "", 100_000, at(10, 20, 9, 5), at=at(10, 20, 9, 5))  # fmt: skip  # noqa: E501
    with pytest.raises(services.BillzSaleConflict, match="store sale"):
        services.link_billz_sale(sale.purchase_id, billz_sale_id="S-55")
    with pytest.raises(services.PurchaseNotFound):
        services.link_billz_sale(10**9, billz_sale_id="x")
    # a sale id that already belongs to another purchase is a conflict, not an IntegrityError
    other = paid_order(customer, created_at=at(10, 21), items_subtotal=100_000)
    second = services.record_online_purchase(customer.pk, other.pk, 100_000, 0, 0, at(10, 21, 0, 3), billz_order_uuid=uuid4())  # fmt: skip  # noqa: E501
    with pytest.raises(services.BillzSaleConflict, match="another purchase"):
        services.link_billz_sale(second.purchase_id, billz_sale_id="S-55")
    # the sale ref defaults to the sale id; pushed_at defaults to now
    with pinned_clock(at(10, 22, 8)):
        third = services.link_billz_sale(second.purchase_id, billz_sale_id="bz-124")
    assert (third.billz_sale_ref, third.billz_pushed_at) == ("bz-124", at(10, 22, 8))


def test_replays_equal_the_first_result_without_a_credit_entry() -> None:
    """Probe P4 and friends: base/percent/promo_excluded come from the engine, not from the credit entry."""
    seed_policies()
    customer = CustomerFactory.create()
    tiny_order = paid_order(customer, created_at=at(10, 20), items_subtotal=50)
    tiny = services.record_online_purchase(customer.pk, tiny_order.pk, 50, 0, 0, at(10, 20, 0, 3))
    assert (tiny.base, tiny.percent, tiny.cashback_amount, tiny.credited_entry_id) == (50, Decimal("1.00"), 0, None)  # fmt: skip  # noqa: E501
    replay = services.record_online_purchase(customer.pk, tiny_order.pk, 50, 0, 0, at(10, 20, 0, 3))
    assert replace(replay, created=True) == tiny
    promo_only = services.record_store_purchase(customer.pk, "S-PROMO", "", 300_000, at(10, 21), promo_amount=300_000)  # fmt: skip  # noqa: E501
    assert (promo_only.base, promo_only.percent, promo_only.promo_excluded, promo_only.credited_entry_id) == (0, Decimal("1.00"), True, None)  # fmt: skip  # noqa: E501
    again = services.record_store_purchase(
        customer.pk, "S-PROMO", "", 300_000, at(10, 21), promo_amount=300_000
    )
    assert replace(again, created=True) == promo_only
    # earn_on_cashback_paid = false (v2): the replay keeps the cashback-paid part out of the base too
    services.adjust(
        customer.pk, "add", 10_000, "boshlang‘ich", StaffUserFactory.create().pk, at=at(10, 21, 1)
    )
    v2_order = paid_order(customer, created_at=at(10, 21, 2), items_subtotal=80_000, cashback_used=10_000)
    services.create_hold(customer.pk, v2_order.pk, 10_000, v2_order.reservation_expires_at)
    services.capture_hold(v2_order.pk, at=v2_order.paid_at)
    v2 = services.record_online_purchase(customer.pk, v2_order.pk, 80_000, 0, 10_000, v2_order.paid_at, policy_version=2)  # fmt: skip  # noqa: E501
    assert (v2.policy_version, v2.base, v2.cashback_amount) == (2, 70_000, 700)
    v2_again = services.record_online_purchase(customer.pk, v2_order.pk, 80_000, 0, 10_000, v2_order.paid_at)
    assert replace(v2_again, created=True) == v2


def test_replay_for_another_customer_is_a_conflict() -> None:
    seed_policies()
    owner, intruder = CustomerFactory.create(), CustomerFactory.create()
    services.record_store_purchase(owner.pk, "S-77", "S-77", 400_000, at(10, 20), at=at(10, 20))
    with pytest.raises(services.BillzSaleConflict, match="another customer"):
        services.record_store_purchase(intruder.pk, "S-77", "S-77", 400_000, at(10, 20), at=at(10, 20))
    order = paid_order(owner, created_at=at(10, 21), items_subtotal=100_000)
    services.record_online_purchase(owner.pk, order.pk, 100_000, 0, 0, at(10, 21, 0, 3))
    with pytest.raises(services.PurchaseConflict, match=f"Order {order.pk}"):
        services.record_online_purchase(intruder.pk, order.pk, 100_000, 0, 0, at(10, 21, 0, 3))
    assert not LoyaltyAccount.objects.filter(customer=intruder, balance__gt=0).exists()
    assert not Purchase.objects.filter(customer=intruder).exists()
    # a Billz order id that another purchase already carries is a conflict too
    used = Purchase.objects.get(order_id=order.pk).billz_order_uuid
    other = paid_order(owner, created_at=at(10, 22), items_subtotal=100_000)
    with pytest.raises(services.PurchaseConflict, match="Billz order"):
        services.record_online_purchase(
            owner.pk, other.pk, 100_000, 0, 0, at(10, 22, 0, 3), billz_order_uuid=used
        )
    assert not Purchase.objects.filter(order_id=other.pk).exists()


@pytest.mark.parametrize("same_customer", [True, False])
def test_store_sale_committed_between_check_and_insert(monkeypatch: pytest.MonkeyPatch, same_customer: bool) -> None:  # fmt: skip  # noqa: E501
    """The existence check reads a stale snapshot (simulated): the unique violation is mapped, not raised."""
    seed_policies()
    owner, caller = CustomerFactory.create(), CustomerFactory.create()
    first = services.record_store_purchase(owner.pk, "S-99", "S-99", 200_000, at(10, 20), at=at(10, 20))
    original = services._load_purchase
    calls: list[dict[str, Any]] = []

    def stale_then_fresh(**lookup: Any) -> Purchase | None:
        calls.append(lookup)
        return None if len(calls) == 1 else original(**lookup)

    monkeypatch.setattr(services, "_load_purchase", stale_then_fresh)
    who = owner if same_customer else caller
    if same_customer:
        raced = services.record_store_purchase(who.pk, "S-99", "S-99", 200_000, at(10, 20), at=at(10, 20))
        assert not raced.created and replace(raced, created=True) == first
    else:
        with pytest.raises(services.BillzSaleConflict, match="another customer"):
            services.record_store_purchase(who.pk, "S-99", "S-99", 200_000, at(10, 20), at=at(10, 20))
    assert calls == [{"billz_sale_id": "S-99"}, {"billz_sale_id": "S-99"}]
    assert Purchase.objects.count() == 1 and LedgerEntry.objects.count() == 1  # the savepoint rolled back


def test_online_order_committed_between_check_and_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_policies()
    owner, caller = CustomerFactory.create(), CustomerFactory.create()
    order = paid_order(owner, created_at=at(10, 21), items_subtotal=100_000)
    first = services.record_online_purchase(owner.pk, order.pk, 100_000, 0, 0, at(10, 21, 0, 3))
    original = services._load_purchase
    calls: list[dict[str, Any]] = []

    def stale_once(
        **lookup: Any,
    ) -> Purchase | None:  # every call's existence check is stale, the re-read not
        calls.append(lookup)
        return None if len(calls) % 2 else original(**lookup)

    monkeypatch.setattr(services, "_load_purchase", stale_once)
    raced = services.record_online_purchase(owner.pk, order.pk, 100_000, 0, 0, at(10, 21, 0, 3))
    assert replace(raced, created=True) == first
    with pytest.raises(services.PurchaseConflict):
        services.record_online_purchase(caller.pk, order.pk, 100_000, 0, 0, at(10, 21, 0, 3))
    assert calls == [{"order_id": order.pk}] * 4 and Purchase.objects.count() == 1


def test_reverse_purchase_links_a_credit_reversal_posted_earlier() -> None:
    """Probe P3: the credit was reversed through post_entry before reverse_purchase ran."""
    seed_policies()
    customer = CustomerFactory.create()
    sale = services.record_store_purchase(customer.pk, "S-7", "S-7", 412_000, at(10, 9, 13, 40), at=at(10, 9, 13, 40))  # fmt: skip  # noqa: E501
    assert sale.credited_entry_id is not None
    account_id = services.get_or_create_account(customer.pk)
    early = services.post_entry(
        account_id,
        kind="reversal",
        amount=-4_120,
        source_type="billz_sale",
        purchase_id=sale.purchase_id,
        source_ref="S-7",
        reverses_id=sale.credited_entry_id,
        at=at(10, 9, 14),
    )
    result = services.reverse_purchase(sale.purchase_id, at(10, 10))
    assert result.reversal_entry_ids == () and result.credit_reversal_entry_id == early.entry_id
    assert result.net_amount == 0 and not result.already_reversed
    purchase = Purchase.objects.get(pk=sale.purchase_id)
    assert (purchase.reversed, purchase.reversal_entry_id) == (True, early.entry_id)
    assert tasks.reconcile_balances_now()["checks"]["purchase_links"] == 0
    # a purchase already flagged reversed without its link (older data) is repaired by a re-run
    Purchase.objects.filter(pk=sale.purchase_id).update(reversal_entry_id=None)
    repaired = services.reverse_purchase(sale.purchase_id, at(10, 11))
    assert repaired.already_reversed and repaired.credit_reversal_entry_id == early.entry_id
    assert Purchase.objects.get(pk=sale.purchase_id).reversal_entry_id == early.entry_id


def test_record_billz_order_persists_the_sale_without_marking_the_push() -> None:
    """M3 live push (design "Billz sale"): the Billz sale id is stored the moment Billz created it; the push
    counts as done only once the sale is paid (``link_billz_sale``)."""
    seed_policies()
    customer = CustomerFactory.create()
    _, result = paid_order_with_cashback(customer, items_subtotal=185_000, promo=0, cashback_used=0, when=(10, 20, 9, 0))  # fmt: skip  # noqa: E501
    uuid = result.billz_order_uuid
    assert uuid is not None
    sale_id = "38c3920a-ffdd-4233-a823-8af3266f5f4f"
    recorded = services.record_billz_order(result.purchase_id, billz_order_uuid=uuid, billz_sale_id=sale_id, billz_sale_ref="5632631379")  # fmt: skip  # noqa: E501
    assert recorded == services.LinkResult(result.purchase_id, uuid, sale_id, "5632631379", None, True)
    info = selectors.billz_push_info(result.purchase_id)
    assert info is not None and (info.billz_sale_id, info.billz_pushed_at) == (sale_id, None)
    again = services.record_billz_order(result.purchase_id, billz_order_uuid=uuid, billz_sale_id=sale_id, billz_sale_ref="5632631379")  # fmt: skip  # noqa: E501
    assert not again.changed
    with pytest.raises(services.BillzSaleConflict, match="already linked"):
        services.record_billz_order(result.purchase_id, billz_order_uuid=uuid, billz_sale_id="other-sale", billz_sale_ref="1")  # fmt: skip  # noqa: E501
    with pytest.raises(services.BillzSaleConflict, match="pushed as Billz order"):
        services.record_billz_order(result.purchase_id, billz_order_uuid=uuid4(), billz_sale_id=sale_id, billz_sale_ref="1")  # fmt: skip  # noqa: E501
    with pytest.raises(ValueError, match="needs the Billz sale id"):
        services.record_billz_order(
            result.purchase_id, billz_order_uuid=uuid, billz_sale_id="", billz_sale_ref=""
        )
    with pytest.raises(ValueError):
        services.record_billz_order(result.purchase_id, billz_order_uuid=uuid, billz_sale_id=sale_id, billz_sale_ref="r" * 33)  # fmt: skip  # noqa: E501
    # the paid sale then completes the link: the same id, the push instant set
    with pinned_clock(at(10, 20, 9, 2)):
        paid = services.link_billz_sale(result.purchase_id, billz_order_uuid=uuid, billz_sale_id=sale_id, billz_sale_ref="5632631379")  # fmt: skip  # noqa: E501
    assert (paid.billz_sale_id, paid.billz_pushed_at, paid.changed) == (sale_id, at(10, 20, 9, 2), True)
    assert LedgerEntry.objects.count() == 1  # never credits


def test_the_billz_payment_attempt_is_persisted_and_forgotten_only_for_its_sale() -> None:
    """Review B2: the push journal stamps the payment attempt before ``POST /v2/order-payment`` (committed),
    and clears it only when Billz certainly did not take the payment."""
    seed_policies()
    customer = CustomerFactory.create()
    _, result = paid_order_with_cashback(customer, items_subtotal=185_000, promo=0, cashback_used=0, when=(10, 20, 9, 0))  # fmt: skip  # noqa: E501
    uuid = result.billz_order_uuid
    assert uuid is not None
    sale_id = "38c3920a-ffdd-4233-a823-8af3266f5f4f"
    with pytest.raises(services.BillzSaleConflict, match="not linked to Billz sale"):
        services.record_billz_payment_attempt(result.purchase_id, billz_sale_id=sale_id, at=at(10, 20, 9, 1))
    services.record_billz_order(result.purchase_id, billz_order_uuid=uuid, billz_sale_id=sale_id, billz_sale_ref="5632631379")  # fmt: skip  # noqa: E501
    services.record_billz_payment_attempt(result.purchase_id, billz_sale_id=sale_id, at=at(10, 20, 9, 1))
    info = selectors.billz_push_info(result.purchase_id)
    assert info is not None and info.billz_payment_attempted_at == at(10, 20, 9, 1)
    with pytest.raises(services.BillzSaleConflict):
        services.clear_billz_payment_attempt(result.purchase_id, billz_sale_id="another-sale")
    services.clear_billz_payment_attempt(result.purchase_id, billz_sale_id=sale_id)
    info = selectors.billz_push_info(result.purchase_id)
    assert info is not None and info.billz_payment_attempted_at is None
    with pytest.raises(services.PurchaseNotFound):
        services.clear_billz_payment_attempt(0, billz_sale_id=sale_id)
    services.record_billz_payment_attempt(result.purchase_id, billz_sale_id=sale_id, at=at(10, 20, 9, 2))
    with pinned_clock(at(10, 20, 9, 3)):
        services.link_billz_sale(result.purchase_id, billz_order_uuid=uuid, billz_sale_id=sale_id)
    with pytest.raises(services.BillzSaleConflict, match="pushed to Billz already"):
        services.record_billz_payment_attempt(result.purchase_id, billz_sale_id=sale_id, at=at(10, 20, 9, 4))
    services.clear_billz_payment_attempt(result.purchase_id, billz_sale_id=sale_id)  # pushed: kept as history
    assert Purchase.objects.get(pk=result.purchase_id).billz_payment_attempted_at == at(10, 20, 9, 2)
