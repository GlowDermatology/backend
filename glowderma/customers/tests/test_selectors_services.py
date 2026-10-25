"""Unit checks of ``customers.selectors`` / ``customers.services`` beyond the API tests."""

from __future__ import annotations

from typing import Any

import pytest

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import DeviceFactory
from glowderma.customers import selectors, services
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.customers.tests.helpers import make_customer
from glowderma.loyalty.services import AdjustmentValidationError

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
