"""The cashback engine — pure functions, integer math only (design 02 §1.9.1, contract §2.7).

::

    bp(percent)                 = core.money.percent_to_bp(percent)      Decimal('1.00') → 100, '0.57' → 57
    eligible                    = max(0, amount − promo − (0 if earn_on_cashback_paid else cashback_paid))
    lifetime_basis              = lifetime_before            (basis before_purchase)
                                = lifetime_before + amount   (basis with_purchase)
    rate_for(lifetime, policy)  = first tier (sorted by from) with from ≤ lifetime < (to or ∞), else None
    credit                      = eligible × bp // 10_000   (0 when no tier covers the lifetime basis)

No floats anywhere: the frontend preview (``frontend/src/domain/cashback.ts``) must produce the same numbers
for the same inputs. The engine never touches the database: :func:`rules_for` snapshots a ``CashbackPolicy``
row and its tiers into a :class:`PolicyRules` value once, and everything else works on plain values.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from glowderma.core import enums
from glowderma.core.money import apply_bp, percent_to_bp

PERCENT_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True, order=True)
class TierBand:
    """One band ``[from_amount, to_amount)`` with its percent (``to_amount`` ``None`` = open-ended)."""

    from_amount: int
    to_amount: int | None
    percent: Decimal

    def covers(self, lifetime: int) -> bool:
        return self.from_amount <= lifetime and (self.to_amount is None or lifetime < self.to_amount)


@dataclass(frozen=True, slots=True)
class PolicyRules:
    """The engine inputs of one policy version (``version`` is informational: ``None`` = no policy)."""

    basis: str
    earn_on_cashback_paid: bool
    tiers: tuple[TierBand, ...]  # sorted by from_amount
    version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tiers", tuple(sorted(self.tiers, key=lambda t: t.from_amount)))
        if self.basis not in enums.RateBasis.values:
            raise ValueError(f"Unknown rate basis {self.basis!r}")


@dataclass(frozen=True, slots=True)
class CreditResult:
    """What :func:`credit_for_purchase` computed; ``credit == 0`` means "post no ledger entry"."""

    eligible: int  # the base the percent was applied to
    lifetime_basis: int  # the lifetime total the tier was picked for
    percent: Decimal | None  # 2-dp tier percent, None = no tier covers the lifetime basis
    credit: int  # so'm, floored
    promo_excluded: bool  # promo lines were excluded from the base


class _TierLike(Protocol):
    from_amount: int
    to_amount: int | None
    percent: Decimal


class _PolicyLike(Protocol):
    version: int
    basis: str
    earn_on_cashback_paid: bool


def rules_for(policy: _PolicyLike, tiers: Iterable[_TierLike]) -> PolicyRules:
    """Snapshot a policy row and its tier rows (already loaded) into a :class:`PolicyRules` value."""
    return PolicyRules(
        basis=str(policy.basis),
        earn_on_cashback_paid=bool(policy.earn_on_cashback_paid),
        tiers=tuple(TierBand(t.from_amount, t.to_amount, Decimal(t.percent)) for t in tiers),
        version=policy.version,
    )


def tier_index(tiers: tuple[TierBand, ...], lifetime: int) -> int | None:
    """Index of the band covering ``lifetime`` in the sorted ``tiers``, or ``None``."""
    for index, tier in enumerate(tiers):
        if tier.covers(lifetime):
            return index
    return None


def tier_for_lifetime(policy: PolicyRules | None, lifetime: int) -> TierBand | None:
    """The band covering ``lifetime`` (inclusive lower, exclusive upper bound), or ``None``."""
    if policy is None:
        return None
    index = tier_index(policy.tiers, lifetime)
    return None if index is None else policy.tiers[index]


def rate_for(lifetime_total: int, policy: PolicyRules | None) -> Decimal | None:
    """The tier percent for ``lifetime_total`` as a 2-dp ``Decimal`` (``None`` = no tier covers it)."""
    tier = tier_for_lifetime(policy, lifetime_total)
    return None if tier is None else tier.percent.quantize(PERCENT_QUANTUM)


def eligible_base(amount: int, promo_amount: int, cashback_paid: int, policy: PolicyRules | None) -> int:
    """The earning part of a purchase: never promo lines; the cashback-paid part only when the policy says so.

    Without a policy nothing earns, but the base is still reported the strict way (``earn = False``).
    """
    if min(amount, promo_amount, cashback_paid) < 0:
        raise ValueError("Amounts must be non-negative")
    earn_on_paid = policy is not None and policy.earn_on_cashback_paid
    return max(0, amount - promo_amount - (0 if earn_on_paid else cashback_paid))


def lifetime_basis(policy: PolicyRules | None, lifetime_before: int, amount: int) -> int:
    """The lifetime total that picks the tier: before this purchase, or including it (``with_purchase``)."""
    if policy is not None and policy.basis == enums.RateBasis.WITH_PURCHASE:
        return lifetime_before + amount
    return lifetime_before


def credit_for_purchase(
    *, amount: int, promo_amount: int, cashback_paid: int, lifetime_before: int, policy: PolicyRules | None
) -> CreditResult:
    """Credit of one purchase under ``policy``: ``(eligible × bp) // 10 000``, 0 without a covering tier."""
    eligible = eligible_base(amount, promo_amount, cashback_paid, policy)
    basis = lifetime_basis(policy, lifetime_before, amount)
    percent = rate_for(basis, policy)
    credit = 0 if percent is None else apply_bp(eligible, percent_to_bp(percent))
    return CreditResult(
        eligible=eligible,
        lifetime_basis=basis,
        percent=percent,
        credit=credit,
        promo_excluded=promo_amount > 0,
    )


__all__ = [
    "PERCENT_QUANTUM",
    "CreditResult",
    "PolicyRules",
    "TierBand",
    "credit_for_purchase",
    "eligible_base",
    "lifetime_basis",
    "rate_for",
    "rules_for",
    "tier_for_lifetime",
    "tier_index",
]
