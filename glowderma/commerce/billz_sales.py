"""Pushing a paid online order to Billz (design 03 §5 ``billz.push_sale``): commerce's side of the Billz port.

The push itself — create the Billz sale, upsert its items, link the client, read the bill, pay it without
cashback — is ``integrations.billz``'s :meth:`BillzPort.push_sale` (``LiveBillz`` for ``BILLZ_MODE=live``,
``FakeBillz`` otherwise; docs/integrations/billz.md). What commerce adds around it lives here:

* :func:`get_sale_port` — the ``BILLZ_MODE`` adapter over the order's fake scenario
  (``metadata["fake"]["billz"]``);
* :class:`PurchaseJournal` — the push's ``SaleJournal`` over the online purchase: it re-reads the purchase
  under the adapter's sale lock (``progress``), persists the Billz sale id the moment Billz created it
  (``loyalty.services.record_billz_order``), and right before the payment re-checks the order under its
  lock (cancelled → ``SaleCancelled``; an attempt stamped meanwhile by another attempt →
  ``BillzPaymentPending``, a compare-and-set) and commits the payment attempt
  (``loyalty.services.record_billz_payment_attempt``), forgetting it when Billz certainly did not take it;
* :func:`sale_request` — the port's request built from the order and its customer (each line at the
  ``unit_price`` the customer was charged: the Billz sale records what the customer paid, decision D1);
* :func:`record_sale_by_hand` — ``manage.py billz_mark_recorded``: staff entered a dead sale in Billz by
  hand; the purchase is marked recorded (audited ``billz.sale_recorded_by_hand``), so the push and a requeue
  skip it and its units stop counting against availability after the next stock read.

Fake rules (M2, now ``FakeBillz.push_sale``): no scenario → accepted and no Billz sale id echoed
(``billz_sale_id = ""``: the purchase is linked by its ``billz_order_uuid`` only); ``{"push": "down"}`` →
:data:`BillzPushUnavailable` (transient: the outbox retries, ten failures make the message dead);
``{"push": "rejected"}`` → ``BillzRejected`` (permanent: the message is dead at once).

Pushed sales are ``without_cashback`` (design spec "Billz sale"): cashback lives in Glow Derma's ledger only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction

from glowderma.commerce import services, topics
from glowderma.commerce.models import Order
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.models import OutboxMessage
from glowderma.integrations.billz.ports import (
    BillzPaymentPending,
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

logger = logging.getLogger(__name__)

# M2's name for a transient push failure (transport error, Billz down): the outbox retries it.
BillzPushUnavailable = BillzUnavailable
# How long the payment re-check waits for the Order row (409 Conflict after it: the outbox retries).
ORDER_LOCK_WAIT = timedelta(seconds=20)
# A payment attempt stamped by another attempt meanwhile: look at the sale again this much later.
PAYMENT_RACE_RETRY = timedelta(minutes=1)


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
            if info is not None and info.billz_payment_attempted_at is not None:
                # compare-and-set (review NEW-5): another attempt stamped (and may have sent) a payment since
                # this one read the purchase — never a second payment; the next attempt reads the sale first
                raise BillzPaymentPending(
                    f"a payment of the Billz sale of order {self.order_id} was already attempted at "
                    f"{info.billz_payment_attempted_at.isoformat()}; not paying again",
                    retry_after=PAYMENT_RACE_RETRY,
                )
            sale_id = info.billz_sale_id if info is not None else ""
            loyalty_services.record_billz_payment_attempt(
                self.purchase_id, billz_sale_id=sale_id, at=timeutils.now()
            )

    def payment_not_applied(self) -> None:
        info = loyalty_selectors.billz_push_info(self.purchase_id)
        if info is not None and info.billz_sale_id:
            loyalty_services.clear_billz_payment_attempt(self.purchase_id, billz_sale_id=info.billz_sale_id)


# --- a dead sale staff entered in Billz by hand (review NEW-2) ----------------------------------------------

AUDIT_RECORDED_BY_HAND = "billz.sale_recorded_by_hand"
RECORDED_BY_HAND_REASON = "manage.py billz_mark_recorded"
REF_MAX = 32  # Purchase.billz_sale_ref


class RecordByHandRefused(Exception):
    """``billz_mark_recorded`` refused: the message says why and what to do instead."""


@dataclass(frozen=True, slots=True)
class HandRecord:
    """What ``record_sale_by_hand`` recorded (or, with ``dry_run``, would record)."""

    order_id: int
    purchase_id: int
    billz_sale_id: str  # "" when staff know no Billz sale id (the purchase is linked by its order UUID)
    billz_sale_ref: str
    draft_id: str  # the Billz draft the push had created, if any
    payment_attempted_at: datetime | None
    push_status: str  # the billz.push_sale message's status ("" when none)
    dry_run: bool


def _ref(sale_id: str) -> str:
    return sale_id if len(sale_id) <= REF_MAX else sale_id.replace("-", "")[:REF_MAX]


def record_sale_by_hand(
    order_id: int, *, actor: Any, billz_sale_id: str = "", dry_run: bool = False, at: datetime | None = None
) -> HandRecord:
    """Record that staff entered the order's sale in Billz by hand (the remedy of a dead ``billz.push_sale``).

    Under the Order lock: refuses a missing or cancelled order (a sale Billz holds for it is refunded by
    hand), an order without an online purchase, a purchase already recorded in Billz, a push running right now
    (its outbox message ``in_flight``) and a sale id other than the Billz draft the push created. Otherwise it
    sets the purchase's pushed marker through the push's own link path (``loyalty.services.link_billz_sale``:
    ``billz_pushed_at`` = now, the sale id when known), writes ``billz_sale_recorded`` once (system, as the
    push does) and the audit row ``billz.sale_recorded_by_hand`` (the staff actor, the purchase before/after).
    From then on the push skips the purchase (a requeue too), and its units stop counting against
    availability after the next Billz stock read (``services.settle_billz_reservations``).
    """
    instant = at or timeutils.now()
    with transaction.atomic():
        try:
            order = services.lock_order(order_id, wait=services.ADMIN_LOCK_WAIT)
        except services.OrderNotFound as exc:
            raise RecordByHandRefused(f"order {order_id} does not exist") from exc
        except Conflict as exc:
            raise RecordByHandRefused(
                f"order {order_id} is busy (another change holds it); try again"
            ) from exc
        if order.status == enums.OrderStatus.CANCELLED:
            raise RecordByHandRefused(
                f"order {order_id} is cancelled: there is no sale to record "
                "(refund a sale Billz holds by hand)"
            )
        row = loyalty_selectors.purchase_for_order(order_id)
        info = loyalty_selectors.billz_push_info(row.id) if row is not None else None
        if info is None:
            raise RecordByHandRefused(f"order {order_id} has no online purchase (it was never paid)")
        if info.billz_pushed_at is not None:
            raise RecordByHandRefused(
                f"order {order_id} is already recorded in Billz (at {info.billz_pushed_at.isoformat()}, "
                f"sale {info.billz_sale_id or '-'}): nothing to do"
            )
        message = (
            OutboxMessage.objects.filter(topic=topics.BILLZ_PUSH_SALE, payload__order_id=order_id)
            .order_by("-id")
            .first()
        )
        push_status = message.status if message is not None else ""
        if push_status == "in_flight":
            raise RecordByHandRefused(
                f"a Billz push of order {order_id} is running right now; wait for it to end and look again"
            )
        sale_id = billz_sale_id.strip()
        if sale_id and info.billz_sale_id and sale_id != info.billz_sale_id:
            raise RecordByHandRefused(
                f"the purchase of order {order_id} holds the Billz sale {info.billz_sale_id} (the push's "
                f"draft): pass that id, or none"
            )
        sale_id = sale_id or info.billz_sale_id
        ref = info.billz_sale_ref if sale_id == info.billz_sale_id and info.billz_sale_ref else _ref(sale_id)
        record = HandRecord(
            order_id=order_id,
            purchase_id=info.purchase_id,
            billz_sale_id=sale_id,
            billz_sale_ref=ref,
            draft_id=info.billz_sale_id,
            payment_attempted_at=info.billz_payment_attempted_at,
            push_status=push_status,
            dry_run=dry_run,
        )
        if dry_run:
            return record
        try:
            loyalty_services.link_billz_sale(
                info.purchase_id,
                billz_order_uuid=info.billz_order_uuid,
                billz_sale_id=sale_id,
                billz_sale_ref=ref,
                pushed_at=instant,
            )
        except (loyalty_services.BillzSaleConflict, ValueError) as exc:
            raise RecordByHandRefused(f"order {order_id}: {exc}") from exc
        if not order.events.filter(type=enums.OrderEventType.BILLZ_SALE_RECORDED).exists():
            services.record_event(
                order,
                enums.OrderEventType.BILLZ_SALE_RECORDED,
                {"billz_order_id": sale_id or str(info.billz_order_uuid or "")},
                at=instant,
            )
        audit.record(
            action=AUDIT_RECORDED_BY_HAND,
            entity_type="order",
            entity_id=str(order_id),
            actor=actor,
            before={
                "purchase_id": info.purchase_id,
                "billz_sale_id": info.billz_sale_id,
                "billz_pushed_at": None,
                "billz_payment_attempted_at": _iso(info.billz_payment_attempted_at),
                "push_status": push_status,
            },
            after={
                "purchase_id": info.purchase_id,
                "billz_sale_id": sale_id,
                "billz_sale_ref": ref,
                "billz_pushed_at": instant.isoformat(),
            },
            reason=RECORDED_BY_HAND_REASON,
        )
        products = list(order.reservations.values_list("product_id", flat=True).distinct())
        services.settle_billz_reservations(products, at=instant)
    logger.warning(
        "billz.sale_recorded_by_hand order_id=%s purchase_id=%s billz_order_id=%s",
        order_id,
        info.purchase_id,
        sale_id or "-",
    )
    return record


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


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


__all__ = [
    "BillzPushUnavailable",
    "HandRecord",
    "PurchaseJournal",
    "RecordByHandRefused",
    "get_sale_port",
    "record_sale_by_hand",
    "sale_request",
]
