"""Pushing an online sale to Billz (design 03 §5 ``billz.push_sale``) — a thin adapter on ``commerce``'s side.

``integrations.billz``'s port (B3) covers the catalog side only (product lookup, imports) and has no
"create sale" call yet; ``commerce`` may not edit it in this slice, so the push lives behind this small port
until the Billz port gains ``create_sale`` (reported to the lead). The adapter follows ``BILLZ_MODE`` like the
catalog side: ``fake`` answers deterministically, ``live`` raises ``NotImplementedError("M3")``.

Fake rules (``Order.metadata["fake"]["billz"]``):

* absent → the sale is accepted; Billz echoes no sale id (``billz_sale_id = ""``): the online purchase is
  linked by the pre-persisted Billz order UUID only;
* ``{"push": "down"}`` → :class:`BillzPushUnavailable` (transport error: the outbox retries, ten failures make
  the message dead).

Pushed sales are ``without_cashback`` (design spec "Billz sale"): cashback lives in Glow Derma's ledger only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from glowderma.integrations.billz.registry import billz_mode


@dataclass(frozen=True, slots=True)
class SaleLine:
    billz_id: str
    quantity: int
    unit_price: int


@dataclass(frozen=True, slots=True)
class SaleReceipt:
    billz_sale_id: str  # "" when Billz does not echo one


class BillzPushUnavailable(Exception):
    """Billz did not answer; the push is retried by the outbox."""


class BillzSalePort(Protocol):
    def push_sale(
        self, order_uuid: UUID, lines: Sequence[SaleLine], *, without_cashback: bool
    ) -> SaleReceipt:
        """Record the paid online order in Billz, de-duplicated by ``order_uuid`` on Billz's side."""
        ...


class FakeBillzSales:
    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = scenario or {}

    def push_sale(
        self, order_uuid: UUID, lines: Sequence[SaleLine], *, without_cashback: bool
    ) -> SaleReceipt:
        if self.scenario.get("push") == "down":
            raise BillzPushUnavailable("Billz did not answer (fake: down)")
        return SaleReceipt(billz_sale_id="")


def get_sale_port(scenario: Mapping[str, Any] | None = None) -> BillzSalePort:
    mode = billz_mode()
    if mode == "fake":
        return FakeBillzSales(scenario)
    if mode == "live":
        raise NotImplementedError("M3")
    raise ValueError(f"Unknown BILLZ_MODE {mode!r}")
