"""Courier pricing policy (plan decision 5, §3.3). Pure integer math in basis points: no floats, no clock.

The fee a customer pays is fixed at checkout and never changes (no surcharge, no automatic refund of a
difference). At dispatch the binding Yandex offer is compared with the checkout estimate and the paid fee:
within the configured limits the courier is summoned, beyond them staff review the price first.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ROUNDING_STEP = 500  # so'm: fees are shown and charged in multiples of this, rounded up
_BP = 10_000  # basis points in 100 %

DecisionAction = Literal["accept", "review"]
REASON_WITHIN_LIMITS = "within_limits"
REASON_APPROVED = "approved"
REASON_ABOVE_APPROVED = "above_approved"
REASON_INCREASE_OVER_LIMIT = "increase_over_limit"
REASON_LOSS_OVER_LIMIT = "loss_over_limit"


def percent_bp(percent: Decimal) -> int:
    """``Decimal`` percent (0..100, at most two decimals) → basis points; anything else raises."""
    if not isinstance(percent, Decimal) or not percent.is_finite() or not 0 <= percent <= 100:
        raise ValueError("percent must be a finite Decimal in [0, 100]")
    exponent = percent.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise ValueError("percent allows at most two decimal places")
    return int(percent * 100)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def customer_fee(
    carrier_price: int, *, markup_percent: Decimal, vat_percent: Decimal, step: int = ROUNDING_STEP
) -> int:
    """The delivery fee shown at checkout: ``carrier_price × (1 + vat) × (1 + markup)``, rounded **up** to
    ``step`` so'm. ``vat_percent`` is the uplift for an estimate that may exclude VAT (the docs do not say
    whether ``check-price`` does; 0 when it is known to include it)."""
    if isinstance(carrier_price, bool) or not isinstance(carrier_price, int) or carrier_price < 0:
        raise ValueError("carrier_price must be a non-negative integer (so'm)")
    if isinstance(step, bool) or not isinstance(step, int) or step < 1:
        raise ValueError("step must be a positive integer (so'm)")
    numerator = carrier_price * (_BP + percent_bp(vat_percent)) * (_BP + percent_bp(markup_percent))
    return _ceil_div(numerator, _BP * _BP * step) * step


@dataclass(frozen=True, slots=True)
class Decision:
    """What dispatch does with a binding offer: ``accept`` it, or hold it for staff ``review``.

    ``increase`` = offer − checkout estimate and ``loss`` = offer − paid fee (both so'm, never below 0);
    ``reason`` is one of the ``REASON_*`` codes (it becomes the review issue's code).
    """

    action: DecisionAction
    increase: int
    loss: int
    reason: str


def dispatch_decision(
    *,
    quoted_carrier_price: int,
    dispatch_price: int,
    paid_fee: int,
    max_auto_increase: int,
    max_loss_per_shipment: int,
    approved_price: int | None = None,
) -> Decision:
    """Accept the binding offer ``dispatch_price`` (VAT-inclusive) or send it to review.

    A price staff approved (``approved_price``) is accepted up to that amount and reviewed above it.
    Otherwise it is accepted while the increase over the checkout estimate is at most ``max_auto_increase``
    and the loss against the paid fee at most ``max_loss_per_shipment`` (both bounds inclusive).
    """
    for name, value in (
        ("quoted_carrier_price", quoted_carrier_price),
        ("dispatch_price", dispatch_price),
        ("paid_fee", paid_fee),
        ("max_auto_increase", max_auto_increase),
        ("max_loss_per_shipment", max_loss_per_shipment),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer (so'm)")
    increase = max(0, dispatch_price - quoted_carrier_price)
    loss = max(0, dispatch_price - paid_fee)
    if approved_price is not None:
        if dispatch_price <= approved_price:
            return Decision("accept", increase, loss, REASON_APPROVED)
        return Decision("review", increase, loss, REASON_ABOVE_APPROVED)
    if increase > max_auto_increase:
        return Decision("review", increase, loss, REASON_INCREASE_OVER_LIMIT)
    if loss > max_loss_per_shipment:
        return Decision("review", increase, loss, REASON_LOSS_OVER_LIMIT)
    return Decision("accept", increase, loss, REASON_WITHIN_LIMITS)


__all__ = [
    "REASON_ABOVE_APPROVED",
    "REASON_APPROVED",
    "REASON_INCREASE_OVER_LIMIT",
    "REASON_LOSS_OVER_LIMIT",
    "REASON_WITHIN_LIMITS",
    "ROUNDING_STEP",
    "Decision",
    "DecisionAction",
    "customer_fee",
    "dispatch_decision",
    "percent_bp",
]
