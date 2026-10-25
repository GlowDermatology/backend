"""Shared helpers of the loyalty service tests: design instants, the seeded policy versions, chain reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import seeds
from glowderma.loyalty.models import CashbackPolicy, LedgerEntry
from glowderma.loyalty.tests.factories import ORDER_FACTORY, upper_factory

OrderFactory: Any = upper_factory(ORDER_FACTORY)


def at(month: int, day: int, hour: int = 0, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TASHKENT)


def seed_policies() -> dict[int, CashbackPolicy]:
    """The design versions v1–v3 (03 §7.7) with ``author = NULL``; returns them by version."""
    return {spec.version: seeds._insert(spec, None) for spec in seeds.POLICY_VERSIONS}


def chain(account_id: int) -> list[tuple[str, int, int]]:
    """``(kind, amount, balance_after)`` of the account's entries in posting order."""
    return list(
        LedgerEntry.objects.filter(account_id=account_id)
        .order_by("at", "id")
        .values_list("kind", "amount", "balance_after")
    )


def paid_order(customer: Any, *, created_at: datetime, items_subtotal: int, **overrides: Any) -> Any:
    """A paid (``processing``) order of ``customer`` whose ``paid_at`` is ``created_at + 3 min``."""
    return OrderFactory.create(
        customer=customer, paid=True, created_at=created_at, items_subtotal=items_subtotal, **overrides
    )
