"""Transactional writes of `customers` (contract §2.6): the manual cashback adjustment.

Thin orchestration over ``loyalty.services.adjust``, which runs the contract checks in order (``amount``
1..10 000 000, ``reason`` 3..300 trimmed, then ``subtract`` > ``available`` under the ``LoyaltyAccount``
lock), posts the ``adjustment`` entry through ``post_entry`` and audits ``cashback.adjusted``
``{direction, amount, reason, balance_after, ledger_entry_id}`` with the staff actor in the same transaction
— so this module adds no second audit row. Deleted customers are allowed: the balance is kept for a later
re-registration (decision A1).
"""

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
