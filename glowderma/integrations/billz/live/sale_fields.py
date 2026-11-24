"""Reading a Billz order (its sale state and items) and building a sale: the reference and the aggregated
lines."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from glowderma.integrations.billz.live.product_fields import _number, text_of
from glowderma.integrations.billz.ports import (
    SaleLine,
)

REF_MAX = 32


ZERO_TIME_PREFIX = "0001-"  # Go's zero time: "0001-01-01T00:00:00Z"


type SaleState = Literal["open", "paid", "unclear"]


def _real_time(value: Any) -> bool:
    text = text_of(value)
    return bool(text) and not text.startswith(ZERO_TIME_PREFIX) and text.lower() not in ("null", "0")


def _payments(order: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """``order_detail.order_payments`` (the docs list it in the detail); ``None`` when Billz sent none."""
    detail = order.get("order_detail")
    payments = detail.get("order_payments") if isinstance(detail, Mapping) else None
    if payments is None:
        payments = order.get("order_payments")
    if not isinstance(payments, list):
        return None
    return [payment for payment in payments if isinstance(payment, Mapping)]


def sale_state(order: Mapping[str, Any]) -> SaleState:
    """``paid`` when the sale is closed (a real ``finished_at`` or ``sold_at``) and no empty payment list says
    otherwise; ``open`` when it is not closed and holds no payment; ``unclear`` when the signals contradict
    each other (closed without payments, or payments on an open sale) — never paid again then (review S3)."""
    closed = _real_time(order.get("finished_at")) or _real_time(order.get("sold_at"))
    payments = _payments(order)
    if closed:
        return "paid" if payments is None or payments else "unclear"
    return "unclear" if payments else "open"


def is_finished(order: Mapping[str, Any]) -> bool:
    """A paid (closed) sale (:func:`sale_state`)."""
    return sale_state(order) == "paid"


def order_items(order: Mapping[str, Any] | None) -> dict[str, float]:
    if order is None:
        return {}
    detail = order.get("order_detail")
    items = detail.get("order_items") if isinstance(detail, Mapping) else None
    present: dict[str, float] = {}
    for item in items or []:
        if not isinstance(item, Mapping) or item.get("is_returned"):
            continue
        product_id = text_of(item.get("product_id"))
        quantity = _number(item.get("measurement_value"))
        if product_id and quantity is not None:
            present[product_id.lower()] = present.get(product_id.lower(), 0.0) + quantity
    return present


def phone_digits(phone: str) -> str:
    return "".join(char for char in phone if char in "0123456789")


def sale_ref(order_number: str, billz_order_id: str) -> str:
    """The short reference stored on the purchase (≤ 32): Billz's ``order_number``, else the compact id."""
    number = order_number.strip()
    if 0 < len(number) <= REF_MAX:
        return number
    return billz_order_id.replace("-", "")[:REF_MAX]


def aggregate(lines: Sequence[SaleLine]) -> list[tuple[str, int, int]]:
    """``(billz_id, quantity, amount)`` per product in first-seen order (Billz upserts per product)."""
    totals: dict[str, tuple[int, int]] = {}
    for line in lines:
        quantity, amount = totals.get(line.billz_id, (0, 0))
        totals[line.billz_id] = (quantity + line.quantity, amount + line.quantity * line.unit_price)
    return [(billz_id, quantity, amount) for billz_id, (quantity, amount) in totals.items()]


@dataclass(frozen=True, slots=True)
class ResolvedLine:
    """One product of the sale as Billz receives it: the resolved product UUID and the free unit price."""

    barcode: str
    product_id: str
    quantity: int
    unit_price: int

    @property
    def amount(self) -> int:
        return self.quantity * self.unit_price


__all__ = [
    "REF_MAX",
    "ZERO_TIME_PREFIX",
    "ResolvedLine",
    "SaleState",
    "aggregate",
    "is_finished",
    "order_items",
    "phone_digits",
    "sale_ref",
    "sale_state",
]
