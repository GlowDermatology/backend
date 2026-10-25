"""Builders shared by the customers API tests (rows through the factories and ``loyalty.services``)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import connection

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import enums
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import Purchase
from glowderma.loyalty.tests.factories import PurchaseFactory


def make_customer(name: str = "Mijoz", *, days_ago: int = 30, **kwargs: Any) -> Customer:
    if kwargs.get("status") == enums.CustomerStatus.DELETED:
        kwargs.setdefault("deleted_at", ANCHOR - timedelta(days=1))
    return CustomerFactory.create(name=name, registered_at=ANCHOR - timedelta(days=days_ago), **kwargs)


def store_purchase(customer: Customer, amount: int, **kwargs: Any) -> Purchase:
    """A Billz store sale without a credit (``policy`` null keeps the policy table to the seeded rows)."""
    return PurchaseFactory.create(customer=customer, store=True, amount=amount, policy=None, **kwargs)


def online_purchase(customer: Customer, amount: int = 231_000, **kwargs: Any) -> Purchase:
    """A paid online order's purchase without a credit."""
    return PurchaseFactory.create(customer=customer, amount=amount, policy=None, **kwargs)


def set_balance(customer: Customer, amount: int, actor: StaffUser, *, at: datetime | None = None) -> int:
    """Move the customer's balance by ``amount`` through ``post_entry`` (an admin adjustment)."""
    account_id = loyalty_services.get_or_create_account(customer.pk)
    posted = loyalty_services.post_entry(
        account_id,
        kind=enums.LedgerKind.ADJUSTMENT,
        amount=amount,
        source_type=enums.LedgerSourceType.ADMIN,
        reason="Test tuzatish",
        actor_id=actor.pk,
        at=at,
    )
    return posted.balance_after


# The design 03 §6.3 list SQL: the reference the API orderings are checked against.
LIST_SQL = """
SELECT c.id
FROM accounts_customer c
LEFT JOIN loyalty_loyaltyaccount a ON a.customer_id = c.id
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(p.amount), 0) AS lifetime
    FROM loyalty_purchase p WHERE p.customer_id = c.id AND p.reversed = false) t ON true
ORDER BY {order_by}
"""
SQL_ORDER_BY = {
    "-registered_at": "c.registered_at DESC, c.id DESC",
    "registered_at": "c.registered_at ASC, c.id ASC",
    "name": 'c.name COLLATE "glowderma_ci" ASC, c.id ASC',
    "-name": 'c.name COLLATE "glowderma_ci" DESC, c.id DESC',
    "lifetime_total": "COALESCE(t.lifetime, 0) ASC, c.id ASC",
    "-lifetime_total": "COALESCE(t.lifetime, 0) DESC, c.id DESC",
    "balance": "COALESCE(a.balance, 0) ASC, c.id ASC",
    "-balance": "COALESCE(a.balance, 0) DESC, c.id DESC",
}


def sql_order(ordering: str) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute(LIST_SQL.format(order_by=SQL_ORDER_BY[ordering]))
        return [int(row[0]) for row in cursor.fetchall()]
