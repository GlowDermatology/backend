"""The admin cashback adjustment (admin contract §2.6): a thin orchestration over ``loyalty.services.adjust``,
which runs the contract checks, posts the ``adjustment`` entry and audits ``cashback.adjusted``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from glowderma.customers import selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.selectors import LedgerRow


class CustomerNotFound(Exception):
    """The customer id does not exist (the view answers 404)."""


@dataclass(frozen=True, slots=True)
class AdjustmentOutcome:
    """``POST customers/{id}/cashback-adjustments/`` 201: the new entry, ``balance`` and ``available``."""

    entry: LedgerRow
    balance: int
    available: int


def adjust_cashback(
    customer_id: int,
    *,
    direction: str,
    amount: Any,
    reason: Any,
    actor_id: int,
    request: Any = None,
) -> AdjustmentOutcome:
    """Add or subtract cashback by hand.

    ``amount`` and ``reason`` are passed as received (``loyalty.services.adjust`` decides what "not an int"
    means). Raises :class:`CustomerNotFound`, ``loyalty.services.AdjustmentValidationError`` and
    ``loyalty.services.InsufficientBalance`` (both carry the contract field errors and top-level message).
    """
    if not selectors.customer_exists(customer_id):
        raise CustomerNotFound(customer_id)
    with transaction.atomic():
        result = loyalty_services.adjust(customer_id, direction, amount, reason, actor_id, request=request)
        entry = selectors.ledger_entry(result.entry_id)
    return AdjustmentOutcome(entry=entry, balance=result.balance, available=result.available)


__all__ = [
    "AdjustmentOutcome",
    "CustomerNotFound",
    "adjust_cashback",
]
