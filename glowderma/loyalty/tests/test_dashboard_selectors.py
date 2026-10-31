"""Ops console reads of ``loyalty``: POS purchases by day, the cashback flow, the tier distribution."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import selectors
from glowderma.loyalty.tests.factories import (
    CashbackPolicyFactory,
    LedgerEntryFactory,
    PurchaseFactory,
)

pytestmark = pytest.mark.django_db
DAY = datetime(2026, 10, 30, 0, 0, tzinfo=TASHKENT)


def test_store_purchases_by_day_skip_reversed_and_online(django_assert_num_queries: Any) -> None:
    PurchaseFactory.create(store=True, amount=200_000, at=DAY + timedelta(hours=10))
    PurchaseFactory.create(store=True, amount=50_000, at=DAY + timedelta(hours=20))
    PurchaseFactory.create(store=True, amount=90_000, at=DAY + timedelta(days=1, hours=1))
    PurchaseFactory.create(
        store=True,
        amount=70_000,
        at=DAY + timedelta(hours=11),
        reversed=True,
        reversed_at=DAY + timedelta(hours=12),
    )
    with django_assert_num_queries(1):
        rows = selectors.store_purchases_by_day(DAY, DAY + timedelta(days=2))
    assert rows == {date(2026, 10, 30): (2, 250_000), date(2026, 10, 31): (1, 90_000)}


def test_cashback_flow_sums_credits_debits_and_adjustments(django_assert_num_queries: Any) -> None:
    LedgerEntryFactory.create(amount=1_850, at=DAY + timedelta(hours=1))
    LedgerEntryFactory.create(amount=2_000, at=DAY + timedelta(hours=2))
    LedgerEntryFactory.create(debit=True, amount=-3_000, at=DAY + timedelta(hours=3))
    LedgerEntryFactory.create(adjustment=True, amount=500, at=DAY + timedelta(hours=4))
    LedgerEntryFactory.create(amount=9_999, at=DAY - timedelta(hours=1))  # outside
    with django_assert_num_queries(1):
        flow = selectors.cashback_flow(DAY, DAY + timedelta(days=1))
    assert (flow.issued, flow.redeemed, flow.reversed, flow.adjusted, flow.entries) == (
        3_850,
        3_000,
        0,
        500,
        4,
    )


def test_tier_distribution_counts_active_customers_by_lifetime(django_assert_num_queries: Any) -> None:
    policy = CashbackPolicyFactory.create(tiers=[(1_000_000, 5_000_000, "1.00"), (5_000_000, None, "2.00")])
    small, mid, big = CustomerFactory.create(), CustomerFactory.create(), CustomerFactory.create()
    CustomerFactory.create()  # no purchase: below the first tier
    PurchaseFactory.create(store=True, customer=small, amount=500_000, policy=policy)
    PurchaseFactory.create(store=True, customer=mid, amount=2_000_000, policy=policy)
    PurchaseFactory.create(store=True, customer=mid, amount=1_000_000, policy=policy)
    PurchaseFactory.create(store=True, customer=big, amount=6_000_000, policy=policy)
    PurchaseFactory.create(
        store=True, customer=big, amount=9_000_000, policy=policy, reversed=True, reversed_at=DAY
    )
    deleted = CustomerFactory.create(status=enums.CustomerStatus.DELETED, deleted_at=DAY)
    PurchaseFactory.create(store=True, customer=deleted, amount=7_000_000, policy=policy)
    loaded = selectors.policy_at(policy.effective_from + timedelta(seconds=1))
    with django_assert_num_queries(1):
        distribution = selectors.tier_distribution(loaded)
    assert distribution.policy_version == policy.version
    assert [(tier.from_amount, tier.customers) for tier in distribution.tiers] == [
        (1_000_000, 1),
        (5_000_000, 1),
    ]
    assert distribution.uncovered == 2  # small + the one without purchases


def test_tier_distribution_without_a_policy_is_empty(django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(0):
        distribution = selectors.tier_distribution(None)
    assert (distribution.policy_version, distribution.tiers, distribution.uncovered) == (None, [], 0)
