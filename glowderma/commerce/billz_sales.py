"""Pushing a paid online order to Billz (design 03 §5 ``billz.push_sale``): commerce's side of the Billz port.

The push itself — create the Billz sale, upsert its items, link the client, read the bill, pay it without
cashback — is ``integrations.billz``'s :meth:`BillzPort.push_sale` (``LiveBillz`` for ``BILLZ_MODE=live``,
``FakeBillz`` otherwise; docs/integrations/billz.md). What commerce adds around it lives here:

* :func:`get_sale_port` — the ``BILLZ_MODE`` adapter over the order's fake scenario
  (``metadata["fake"]["billz"]``);
* :class:`PurchaseJournal` — the push's ``SaleJournal`` over the online purchase: it re-reads the purchase
  under the adapter's sale lock (``progress``), persists the Billz sale id the moment Billz created it
  (``loyalty.services.record_billz_order``), and right before the payment re-checks the order under its
  lock (cancelled → ``SaleCancelled``) and commits the payment attempt
  (``loyalty.services.record_billz_payment_attempt``), forgetting it when Billz certainly did not take it;
* :func:`sale_request` — the port's request built from the order and its customer (each line at the
  ``unit_price`` the customer was charged: the Billz sale records what the customer paid, decision D1).

Fake rules (M2, now ``FakeBillz.push_sale``): no scenario → accepted and no Billz sale id echoed
(``billz_sale_id = ""``: the purchase is linked by its ``billz_order_uuid`` only); ``{"push": "down"}`` →
:data:`BillzPushUnavailable` (transient: the outbox retries, ten failures make the message dead);
``{"push": "rejected"}`` → ``BillzRejected`` (permanent: the message is dead at once).

Pushed sales are ``without_cashback`` (design spec "Billz sale"): cashback lives in Glow Derma's ledger only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import transaction

from glowderma.commerce import services
from glowderma.commerce.models import Order
from glowderma.core import enums, timeutils
from glowderma.integrations.billz.ports import (
    BillzPort,
    BillzUnavailable,
    SaleCancelled,
    SaleLine,
    SaleProgress,
    SaleRequest,
)
from glowderma.integrations.billz.registry import get_billz
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services

# M2's name for a transient push failure (transport error, Billz down): the outbox retries it.
BillzPushUnavailable = BillzUnavailable
# How long the payment re-check waits for the Order row (409 Conflict after it: the outbox retries).
ORDER_LOCK_WAIT = timedelta(seconds=20)


class PurchaseJournal:
    """``SaleJournal`` over the online purchase: every write is committed before the adapter goes on."""

    def __init__(self, purchase_id: int, order_uuid: UUID, order_id: int) -> None:
        self.purchase_id = purchase_id
        self.order_uuid = order_uuid
        self.order_id = order_id

    def progress(self) -> SaleProgress:
        info = loyalty_selectors.billz_push_info(self.purchase_id)
        if info is None:
            raise LookupError(f"Purchase {self.purchase_id} does not exist")
        return SaleProgress(
            billz_order_id=info.billz_sale_id,
            billz_sale_ref=info.billz_sale_ref,
            payment_attempted_at=info.billz_payment_attempted_at,
            pushed=info.billz_pushed_at is not None,
        )

    def order_created(self, billz_order_id: str, billz_sale_ref: str) -> None:
        loyalty_services.record_billz_order(
            self.purchase_id,
            billz_order_uuid=self.order_uuid,
            billz_sale_id=billz_order_id,
            billz_sale_ref=billz_sale_ref,
        )

    def before_payment(self) -> None:
        """Under the Order lock (the lock ``cancel_order`` takes): a cancelled order stops the push; otherwise
        the attempt is committed, so a cancel from now on sees a Billz sale that may be paid."""
        with transaction.atomic():
            # bounded: an admin cancel holding the row (a BTS call) makes this attempt fail and retry later,
            # before anything is paid, instead of holding the push past its deadline
            order = services.lock_order(self.order_id, wait=ORDER_LOCK_WAIT)
            if order.status == enums.OrderStatus.CANCELLED:
                raise SaleCancelled(f"order {self.order_id} was cancelled before its Billz sale was paid")
            info = loyalty_selectors.billz_push_info(self.purchase_id)
            sale_id = info.billz_sale_id if info is not None else ""
            loyalty_services.record_billz_payment_attempt(
                self.purchase_id, billz_sale_id=sale_id, at=timeutils.now()
            )

    def payment_not_applied(self) -> None:
        info = loyalty_selectors.billz_push_info(self.purchase_id)
        if info is not None and info.billz_sale_id:
            loyalty_services.clear_billz_payment_attempt(self.purchase_id, billz_sale_id=info.billz_sale_id)


def sale_request(order: Order, order_uuid: UUID) -> SaleRequest:
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
    )


def get_sale_port(scenario: Mapping[str, Any] | None = None) -> BillzPort:
    return get_billz(scenario=scenario)


__all__ = ["BillzPushUnavailable", "PurchaseJournal", "get_sale_port", "sale_request"]
