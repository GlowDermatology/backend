"""Pushing a paid online order to Billz (design 03 §5 ``billz.push_sale``): commerce's side of the Billz port.

The push itself — create the Billz sale, upsert its items, link the client, read the bill, pay it without
cashback — is ``integrations.billz``'s :meth:`BillzPort.push_sale` (``LiveBillz`` for ``BILLZ_MODE=live``,
``FakeBillz`` otherwise; docs/integrations/billz.md). What commerce adds around it lives here:

* :func:`get_sale_port` — the ``BILLZ_MODE`` adapter over the order's fake scenario
  (``metadata["fake"]["billz"]``);
* :class:`PurchaseJournal` — persists the Billz sale id onto the online purchase
  (``loyalty.services.record_billz_order``) the moment Billz created the sale, before the adapter sends
  anything else (design "Billz sale"), so a retry after a crash at any later step resumes on that sale;
* :func:`sale_request` — the port's request built from the order, its purchase and its customer.

Fake rules (M2, now ``FakeBillz.push_sale``): no scenario → accepted and no Billz sale id echoed
(``billz_sale_id = ""``: the purchase is linked by its ``billz_order_uuid`` only); ``{"push": "down"}`` →
:data:`BillzPushUnavailable` (transient: the outbox retries, ten failures make the message dead);
``{"push": "rejected"}`` → ``BillzRejected`` (permanent: the message is dead at once).

Pushed sales are ``without_cashback`` (design spec "Billz sale"): cashback lives in Glow Derma's ledger only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from glowderma.commerce.models import Order
from glowderma.integrations.billz.ports import BillzPort, BillzUnavailable, SaleLine, SaleRequest
from glowderma.integrations.billz.registry import get_billz
from glowderma.loyalty import services as loyalty_services

# M2's name for a transient push failure (transport error, Billz down): the outbox retries it.
BillzPushUnavailable = BillzUnavailable


class PurchaseJournal:
    """``SaleJournal`` over the online purchase: the Billz sale id is committed as soon as it exists."""

    def __init__(self, purchase_id: int, order_uuid: UUID) -> None:
        self.purchase_id = purchase_id
        self.order_uuid = order_uuid

    def order_created(self, billz_order_id: str, billz_sale_ref: str) -> None:
        loyalty_services.record_billz_order(
            self.purchase_id,
            billz_order_uuid=self.order_uuid,
            billz_sale_id=billz_order_id,
            billz_sale_ref=billz_sale_ref,
        )


def sale_request(order: Order, order_uuid: UUID, billz_order_id: str) -> SaleRequest:
    """The order's lines (catalog Billz ids, quantities, the prices the customer paid) and its customer."""
    lines = [
        SaleLine(billz_id=item.billz_id, quantity=item.quantity, unit_price=item.unit_price)
        for item in order.items.order_by("position")
    ]
    return SaleRequest(
        order_id=order.pk,
        order_uuid=order_uuid,
        lines=lines,
        customer_phone=str(order.customer.phone or ""),
        billz_order_id=billz_order_id,
    )


def get_sale_port(scenario: Mapping[str, Any] | None = None) -> BillzPort:
    return get_billz(scenario=scenario)


__all__ = ["BillzPushUnavailable", "PurchaseJournal", "get_sale_port", "sale_request"]
