"""Unit checks of ``customers.selectors`` / ``customers.services`` beyond the API tests."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import DeviceFactory
from glowderma.customers import selectors, services
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.customers.tests.helpers import make_customer, online_purchase, store_purchase
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty.services import AdjustmentValidationError
from glowderma.notifications import selectors as notifications_selectors
from glowderma.notifications.tests.factories import CustomerSegmentFactory, SegmentMemberFactory

pytestmark = pytest.mark.django_db


def test_search_q_ignores_blank_terms() -> None:
    assert selectors.search_q("   ") is None
    assert selectors.search_q("ab") is not None


def test_customers_list_refuses_an_unknown_ordering(clock: Any) -> None:
    with pytest.raises(ValueError, match="ordering"):
        selectors.customers_list(ordering="phone")


def test_active_device_prefers_the_most_recent_active_one(clock: Any) -> None:
    customer = make_customer("Qurilma")
    assert selectors.active_device(customer.pk) is None
    DeviceFactory.create(customer=customer, model="Old", last_active_at=ANCHOR.replace(day=1))
    DeviceFactory.create(customer=customer, model="New", last_active_at=ANCHOR.replace(day=20))
    device = selectors.active_device(customer.pk)
    assert device is not None and device.model == "New"


def test_card_of_an_unknown_customer_is_none(clock: Any) -> None:
    assert selectors.customer_card(999_999) is None
    assert selectors.newest_order(999_999) is None


def test_adjust_cashback_refuses_an_unknown_customer(staff: StaffUser, clock: Any) -> None:
    with pytest.raises(services.CustomerNotFound):
        services.adjust_cashback(999_999, direction="add", amount=1, reason="Sabab", actor_id=staff.pk)


def test_adjust_cashback_maps_through_the_loyalty_rules(staff: StaffUser, clock: Any) -> None:
    customer = make_customer("Mijoz")
    with pytest.raises(AdjustmentValidationError) as raised:
        services.adjust_cashback(customer.pk, direction="add", amount=0, reason="Sabab", actor_id=staff.pk)
    assert raised.value.message == "Summani kiriting."
    outcome = services.adjust_cashback(
        customer.pk, direction="add", amount=10, reason="Sabab", actor_id=staff.pk
    )
    assert (outcome.balance, outcome.available, outcome.entry.amount, outcome.entry.reason) == (
        10,
        10,
        10,
        "Sabab",
    )


def test_card_groups_agree_with_the_notifications_membership_predicate(clock: Any) -> None:
    """``customer_groups`` (one query, rules in Python) answers exactly the §2.8 predicate that
    ``notifications.selectors.segments_for_customer`` evaluates in SQL, rule edge cases included."""
    customers = {
        "none": make_customer("Bo‘sh"),
        "two_online": make_customer("Ikki onlayn"),
        "reversed": make_customer("Qaytarilgan"),
        "deleted": make_customer("", status="deleted"),
    }
    for _ in range(2):
        online_purchase(customers["two_online"], 1_000_000)
    store_purchase(customers["two_online"], 3_000_000)
    online_purchase(customers["reversed"], 9_000_000, reversed=True, reversed_at=ANCHOR)
    online_purchase(customers["deleted"], 5_000_000)
    segments = [
        CustomerSegmentFactory.create(min_online_orders=2, min_lifetime_amount=None),  # ≥ boundary
        CustomerSegmentFactory.create(min_online_orders=3, min_lifetime_amount=None),
        CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=5_000_000),  # = boundary
        CustomerSegmentFactory.create(min_online_orders=2, min_lifetime_amount=5_000_001),  # AND
        CustomerSegmentFactory.create(min_online_orders=1, min_lifetime_amount=None),
        CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=None),  # manual only
    ]
    for key in ("none", "deleted"):
        SegmentMemberFactory.create(segment=segments[5], customer=customers[key])
    SegmentMemberFactory.create(segment=segments[1], customer=customers["reversed"])  # manual beats rules
    for key, customer in customers.items():
        totals = loyalty_selectors.customer_totals(customer.pk)
        got = [group.id for group in selectors.customer_groups(customer, totals)]
        want = [segment.pk for segment in notifications_selectors.segments_for_customer(customer.pk)]
        assert got == want, key
    two_online = selectors.customer_groups(
        customers["two_online"], loyalty_selectors.customer_totals(customers["two_online"].pk)
    )
    assert [group.id for group in two_online] == [segments[0].pk, segments[2].pk, segments[4].pk]
    reversed_only = selectors.customer_groups(
        customers["reversed"], loyalty_selectors.customer_totals(customers["reversed"].pk)
    )
    assert [group.id for group in reversed_only] == [segments[1].pk]  # reversed purchases never count
    assert selectors.customer_groups(customers["deleted"], loyalty_selectors.customer_totals(0)) == []


@pytest.mark.parametrize("reason", ["abc\x00def", "abc\ud800def", "\udfff\udfff\udfff"])
def test_adjustment_reason_refuses_characters_postgres_cannot_store(
    staff: StaffUser, clock: Any, reason: str
) -> None:
    """NUL and lone surrogates answer ``reason`` ``invalid`` before any write (never a DataError → 500)."""
    customer = make_customer("Mijoz")
    with pytest.raises(AdjustmentValidationError) as raised:
        services.adjust_cashback(customer.pk, direction="add", amount=10, reason=reason, actor_id=staff.pk)
    assert raised.value.message == "Sababni kiriting."
    errors = raised.value.errors
    assert [(field, [(e.code, str(e)) for e in details]) for field, details in errors.items()] == [
        ("reason", [("invalid", "Noto‘g‘ri qiymat.")])
    ]
