"""Outbox handlers, dead-letter hooks and the beat task of `commerce` (design 03 §5, §4.1 rows 3/14/15).

Imported at startup by ``CommerceConfig.ready`` so the handlers exist in every process that dispatches the
outbox. Delivery is at-least-once: each handler is idempotent at the provider boundary.

* ``bts.create_shipment`` — the checks under the Order lock (skip unless the order is ``processing`` and the
  shipment is not created yet, skip a request BTS already answered: ``request_no``) are committed **before**
  BTS is called; the ``clientId`` lookup (``BTS_CLIENT_ID_PREFIX`` + order id, from the day before the
  shipment row was created) and the create then run holding only the ``bts-create:{order_id}`` advisory lock
  (a redelivery after "BTS created the parcel, then the worker crashed" adopts it); the order is re-locked to
  record ``shipment_created`` / ``shipment_failed`` (source ``admin`` + actor for a manual retry). For an
  order cancelled meanwhile — before the call or while it was in flight — a parcel found or just created is
  cancelled at BTS (orphan). Only transport errors raise (the dispatcher retries).
* ``billz.push_sale`` — skip when the purchase is already pushed; never call a fake Billz under the
  production settings (customer contract §6.14: WARNING ``billz.sale_not_pushed``, the sale is entered by
  hand) and never complete the sale of an order cancelled meanwhile (checked before the push, again under
  the Order lock right before the payment through ``PurchaseJournal.before_payment``, and after it: a sale
  paid while the order was being cancelled is linked but gets no ``billz_sale_recorded``, ERROR
  ``billz.sale_of_cancelled_order`` asks for a refund by hand); otherwise push through the Billz port, which
  persists the Billz sale id the moment Billz created it (``billz_sales.PurchaseJournal``) so a retry resumes
  on it, then ``loyalty.services.link_billz_sale`` and ``billz_sale_recorded`` once. Billz refusing for good
  (``BillzRejected``: unknown product, a bill that is not what the customer paid, …) dead-letters the
  message at once with the reason (``outbox.PermanentFailure``); a closed shift is deferred every
  ``BILLZ_SHIFT_RETRY_MINUTES`` for ``BILLZ_SHIFT_RETRY_MAX_HOURS`` and a payment whose answer was lost
  waits its quiet period and a sale another worker is pushing (its lock held) is looked at a minute later
  (``outbox.RetryLater``: no attempt spent); any other failure raises and is retried; a conflict raises,
  never a silent success.
* ``refund.submit`` — Click: status check by ``provider_txn_id`` before any reversal; ``mark_submitting``
  (a fenced compare-and-set) is committed before the reversal call; ``succeeded`` / ``failed`` / ``unknown``
  (+ events); ``unknown`` is never retried automatically. Click answering that it did **not** process the
  reversal (``ClickNotProcessed``: a redirect, 408/425/429, an auth refusal, a request that never left)
  releases the claim (``submitting → requested``) and raises, so the outbox retries with a fresh status check.
  **Any other exception** after the claim (an untyped error, a Celery soft time limit) records ``unknown`` and
  re-raises: never a second automatic reversal, never a silent ``submitting``. Raises only on a transport
  error **before** submission (or after a release), so a message that dies always finds the refund
  ``requested``.

Dead letters (``outbox.on_dead``): ``bts.create_shipment`` → shipment ``failed`` (``dead_letter``) +
``shipment_failed``; ``refund.submit`` → refund ``failed`` (``dead_letter``, never ``unknown``) +
``refund_failed``; ``billz.push_sale`` → an ERROR line naming the order and the Billz draft, if any (the
``outbox.dead`` audit row carries the reason; staff complete or record the sale in Billz by hand).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from celery.signals import worker_ready  # type: ignore[import-untyped]
from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction

from glowderma.catalog import topics as catalog_topics
from glowderma.commerce import selectors, services, topics
from glowderma.commerce.billz_sales import PurchaseJournal, get_sale_port, sale_request
from glowderma.commerce.models import Order, StockReservation
from glowderma.core import alerts, audit, enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import InvalidTransition
from glowderma.core.models import OutboxMessage
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as integration_services
from glowderma.integrations.billz.ports import (
    BillzPaymentPending,
    BillzRejected,
    BillzSaleBusy,
    BillzShiftClosed,
    SaleCancelled,
)
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, CreateRequest, Parcel
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click.ports import (
    ClickError,
    ClickNotProcessed,
    ClickRefundRejected,
    ClickUnavailable,
)
from glowderma.integrations.click.registry import get_click
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services

logger = logging.getLogger(__name__)

_E = enums.OrderEventType


def _payload(message: OutboxMessage) -> dict[str, Any]:
    if not isinstance(message.payload, dict):
        raise ValueError(f"Outbox message {message.pk} has no object payload")
    return message.payload


# --- bts.create_shipment ------------------------------------------------------------------------------------


def _shipment_source(payload: dict[str, Any]) -> tuple[bool, int | None]:
    manual = bool(payload.get("manual"))
    actor_id = payload.get("actor_id")
    return manual, (int(actor_id) if manual and actor_id is not None else None)


def _request_no(payload: dict[str, Any]) -> int | None:
    value = payload.get("request_no")
    return None if value is None else int(value)


class CreateBusy(RuntimeError):
    """Another worker is creating this order's parcel right now: the outbox retries this delivery later."""


@contextmanager
def _create_lock(order_id: int) -> Iterator[None]:
    """A session-level advisory lock ``bts-create:{order_id}`` held across the BTS lookup and create, so two
    deliveries of a create request never call BTS for the same order at once. It holds no row and no open
    transaction; a busy lock raises :class:`CreateBusy` (retried)."""
    key = f"bts-create:{order_id}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [key])
        row = cursor.fetchone()
    if not row or not row[0]:
        raise CreateBusy(f"order {order_id}: another worker is creating its BTS parcel")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", [key])


@dataclass(frozen=True, slots=True)
class _CreatePlan:
    """What the checks under the Order lock decided (committed before any BTS call)."""

    order_id: int
    scenario: dict[str, Any]
    since: datetime  # the shipment row's creation: the clientId lookup starts the day before
    request: CreateRequest | None  # None: the order was cancelled meanwhile (orphan lookup only)
    request_no: int | None
    manual: bool
    actor_id: int | None


def _plan_create(payload: dict[str, Any]) -> _CreatePlan | None:
    """Under the Order lock: skip unless the order is ``processing`` (or ``cancelled``: orphan lookup) and the
    shipment is not created yet, and skip a request BTS already answered (``request_no``) or one superseded by
    a newer request."""
    order_id = int(payload["order_id"])
    manual, actor_id = _shipment_source(payload)
    request_no = _request_no(payload)
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.fulfilment != enums.FulfilmentMethod.DELIVERY:
            return None
        shipment = delivery_selectors.shipment_info(order_id)
        if shipment is None or shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return None
        if request_no is not None and (
            request_no < shipment.create_requests
            or delivery_selectors.create_request_answered(order_id, request_no)
        ):
            return None  # answered before, or superseded by a newer (manual) request
        scenario = services.fake_scenario(order, "bts")
        plan = _CreatePlan(order_id, scenario, shipment.created_at, None, request_no, manual, actor_id)
        if order.status == enums.OrderStatus.CANCELLED:
            return plan
        if order.status != enums.OrderStatus.PROCESSING:
            return None  # delivered meanwhile: nothing to create (03 §4.1 row 6)
        request = CreateRequest(
            client_id=str(order_id),
            attempt=shipment.attempts + 1,
            recipient_name=order.recipient_name,
            recipient_phone=order.recipient_phone,
            address=order.address_text,
            region_code=order.address_region_code,
            city_code=order.address_city_code,
            comment=order.address_comment,
        )
        return replace(plan, request=request)


def _cancel_orphan(order: Order, plan: _CreatePlan, parcel: Parcel, now: datetime) -> None:
    """The order was cancelled while this create request was in flight (or BTS created the parcel and the
    worker crashed): the parcel exists although the cancel saw none — cancel it at BTS so no courier collects
    it (under the Order lock: one bounded call; a transport error raises and the outbox retries)."""
    outcome = delivery_services.cancel_orphan(
        order.pk, parcel, scenario=plan.scenario, at=now, request_no=plan.request_no
    )
    if outcome.ok:
        logger.warning(
            "Cancelled the orphan BTS parcel %s of cancelled order %s", parcel.tracking_number, order.pk
        )
    else:
        logger.error(
            "BTS refused to cancel the orphan parcel %s of cancelled order %s: %s",
            parcel.tracking_number,
            order.pk,
            outcome.reason,
        )


def _record_parcel(plan: _CreatePlan, parcel: Parcel) -> None:
    """Re-lock the order and record the parcel BTS holds: ``created`` (+ ``shipment_created``), or — the
    order was cancelled meanwhile — cancel it at BTS as an orphan."""
    order_id = plan.order_id
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        shipment = delivery_selectors.shipment_info(order_id)
        if shipment is None:
            return
        if shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            if shipment.tracking_number and shipment.tracking_number != parcel.tracking_number:
                logger.error(
                    "bts.duplicate_parcels %s",
                    {
                        "order_id": order_id,
                        "recorded": shipment.tracking_number,
                        "found": parcel.tracking_number,
                    },
                )
            return
        if order.status == enums.OrderStatus.CANCELLED:
            _cancel_orphan(order, plan, parcel, now)
            return
        tariff = parcel.tariff if parcel.tariff is not None else order.delivery_fee
        created = delivery_services.mark_created(
            order_id,
            tracking_number=parcel.tracking_number,
            tariff=tariff,
            bts_order_id=parcel.bts_order_id,
            bts_status=parcel.status,
            at=now,
            manual=plan.manual,
            payload={"tracking_number": parcel.tracking_number, "status": parcel.status},
            request_no=plan.request_no,
        )
        if created:
            services.record_event_once(
                order,
                _E.SHIPMENT_CREATED,
                {"tracking_number": parcel.tracking_number, "tariff": tariff, "manual": plan.manual},
                actor_id=plan.actor_id,
                at=now,
            )


def _record_refusal(plan: _CreatePlan, exc: BtsRejected) -> None:
    """Re-lock the order and record BTS's definitive refusal (``failed`` + ``shipment_failed``), unless the
    order or the request moved on meanwhile."""
    order_id = plan.order_id
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.status != enums.OrderStatus.PROCESSING:
            return
        number = delivery_services.mark_failed(
            order_id,
            error_code=exc.error_code,
            error=exc.message,
            at=now,
            manual=plan.manual,
            request_no=plan.request_no,
        )
        if number is not None:
            services.record_event_once(
                order,
                _E.SHIPMENT_FAILED,
                {"attempt": number, "manual": plan.manual, "error_code": exc.error_code},
                actor_id=plan.actor_id,
                at=now,
            )


def create_shipment(payload: dict[str, Any]) -> None:
    """The ``bts.create_shipment`` effect (callable inline by the seeder under a pinned clock).

    Three steps, so no BTS call ever runs under the Order row lock (a Click/Payme webhook, an admin action
    or the customer's order view on the same order never waits for BTS): (1) the checks of
    :func:`_plan_create`, committed; (2) holding only the ``bts-create:{order_id}`` advisory lock, the
    ``clientId`` lookup and — when BTS holds no parcel — the create; (3) re-lock the order and record the
    outcome (a parcel of an order cancelled meanwhile is cancelled at BTS as an orphan). A request BTS
    already answered (its ``request_no`` names a ``created`` / ``create_failed`` shipment event) or one
    superseded by a newer request is skipped without calling BTS: a redelivery after "``mark_failed``
    committed, then the worker died" must not ask BTS a second time.
    """
    order_id = int(payload["order_id"])
    with _create_lock(order_id):
        plan = _plan_create(payload)
        if plan is None:
            return
        port = get_bts(plan.scenario)
        parcel = port.find_by_client_id(str(order_id), since=plan.since)
        if plan.request is None:  # cancelled meanwhile: only a parcel that reached BTS needs cancelling
            if parcel is not None:
                _record_parcel(plan, parcel)
            return
        if parcel is None:
            try:
                parcel = port.create(plan.request)
            except BtsRejected as exc:
                _record_refusal(plan, exc)
                return
        _record_parcel(plan, parcel)


@outbox.register(topics.BTS_CREATE_SHIPMENT)
def handle_bts_create_shipment(message: OutboxMessage) -> None:
    create_shipment(_payload(message))


def on_bts_create_dead(message: OutboxMessage) -> None:
    """Ten transport failures: the shipment is ``failed`` (``dead_letter``) and retryable by the admin.

    Runs once per dead message; every dead create request (the automatic one, then each manual retry) gets
    its own ``shipment_failed {attempt, manual, error_code: dead_letter}``. A second notice of the same
    message is a no-op (``mark_failed`` finds the request answered).
    """
    payload = _payload(message)
    order_id = int(payload["order_id"])
    manual, actor_id = _shipment_source(payload)
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.status != enums.OrderStatus.PROCESSING:
            return
        shipment = Shipment.objects.filter(order_id=order_id).first()
        if shipment is None or shipment.create_message_id not in (None, message.pk):
            return  # a newer create request owns the shipment now
        number = delivery_services.mark_failed(
            order_id,
            error_code=topics.DEAD_LETTER_ERROR_CODE,
            error=message.last_error,
            at=now,
            manual=manual,
            request_no=_request_no(payload),
        )
        if number is not None:
            services.record_event_once(
                order,
                _E.SHIPMENT_FAILED,
                {"attempt": number, "manual": manual, "error_code": topics.DEAD_LETTER_ERROR_CODE},
                actor_id=actor_id,
                at=now,
            )


# --- billz.push_sale ----------------------------------------------------------------------------------------


def push_sale(payload: dict[str, Any]) -> None:
    """The ``billz.push_sale`` effect (callable inline by the seeder under a pinned clock; module doc)."""
    purchase_id = int(payload["purchase_id"])
    order_id = int(payload["order_id"])
    order_uuid = UUID(str(payload["billz_order_uuid"]))
    info = loyalty_selectors.billz_push_info(purchase_id)
    if info is None:
        raise LookupError(f"Purchase {purchase_id} does not exist")
    if info.billz_pushed_at is not None:
        return
    if info.billz_order_uuid != order_uuid:  # a payload of another purchase: refused before Billz is called
        raise loyalty_services.BillzSaleConflict(
            f"Purchase {purchase_id} was pushed as Billz order {info.billz_order_uuid}, not {order_uuid}"
        )
    if integration_modes.fake_in_production("billz"):
        # §6.14: a fake records no Billz sale; billz_pushed_at stays null so the sale is entered by hand — and
        # its units are settled now, so they never start counting at the first live stock read (NEW-2)
        services.settle_order_reservations(order_id)
        logger.warning("billz.sale_not_pushed order_id=%s purchase_id=%s", order_id, purchase_id)
        return
    order = Order.objects.select_related("customer").get(pk=order_id)
    if order.status == enums.OrderStatus.CANCELLED:
        if info.billz_sale_id:
            logger.error(
                "billz.sale_of_cancelled_order order_id=%s billz_order_id=%s: the push stopped; delete the "
                "draft or refund the sale in Billz by hand",
                order_id,
                info.billz_sale_id,
            )
            _alert_cancelled_sale(order_id, purchase_id, "push_stopped")
        else:
            logger.warning("billz.sale_skipped_cancelled order_id=%s purchase_id=%s", order_id, purchase_id)
        return
    port = get_sale_port(services.fake_scenario(order, "billz"))
    try:
        receipt = port.push_sale(
            sale_request(order, order_uuid), PurchaseJournal(purchase_id, order_uuid, order_id)
        )
    except SaleCancelled:
        # cancelled after the push started: nothing was paid; the draft (if any) is named for staff
        draft = loyalty_selectors.billz_push_info(purchase_id)
        logger.error(
            "billz.sale_of_cancelled_order order_id=%s billz_order_id=%s: the order was cancelled during the "
            "push; nothing was paid, delete the draft in Billz",
            order_id,
            draft.billz_sale_id if draft is not None else "",
        )
        _alert_cancelled_sale(order_id, purchase_id, "cancelled_during_push")
        return
    except BillzShiftClosed as exc:
        # decision D2: wait for the next shift without spending the attempts, then give up "shift closed"
        logger.warning(
            "billz.sale_deferred order_id=%s reason=shift_closed error_code=%s", order_id, exc.error_code
        )
        raise outbox.RetryLater(
            timedelta(minutes=settings.BILLZ_SHIFT_RETRY_MINUTES),
            f"shift closed: Billz refused the sale of order {order_id} for now: {exc}",
            give_up_after=timedelta(hours=settings.BILLZ_SHIFT_RETRY_MAX_HOURS),
        ) from exc
    except BillzPaymentPending as exc:
        # review B2: a payment whose answer was lost is looked at again after a quiet period, never resent
        logger.warning("billz.sale_deferred order_id=%s reason=payment_pending", order_id)
        raise outbox.RetryLater(exc.retry_after, f"Billz payment of order {order_id} pending: {exc}") from exc
    except BillzSaleBusy as exc:
        # NEW-9: another worker is pushing this sale (its lock expires by itself): no attempt spent
        logger.info("billz.sale_deferred order_id=%s reason=sale_busy", order_id)
        raise outbox.RetryLater(exc.retry_after, f"Billz sale of order {order_id} busy: {exc}") from exc
    except BillzRejected as exc:
        logger.error("billz.sale_rejected order_id=%s error_code=%s", order_id, exc.error_code)
        raise outbox.PermanentFailure(f"Billz refused the sale of order {order_id}: {exc}") from exc
    now = timeutils.now()
    with transaction.atomic():
        locked = services.lock_order(order_id)
        loyalty_services.link_billz_sale(
            purchase_id,
            billz_order_uuid=order_uuid,
            billz_sale_id=receipt.billz_sale_id,
            billz_sale_ref=receipt.billz_sale_ref,
            pushed_at=now,
        )
        if locked.status == enums.OrderStatus.CANCELLED:
            # cancelled while the payment was in flight (review A S5): Billz holds a paid sale of a cancelled
            # order; no billz_sale_recorded event — staff refund the sale in Billz by hand
            logger.error(
                "billz.sale_of_cancelled_order order_id=%s billz_order_id=%s: the order was cancelled while "
                "its sale was paid in Billz; refund the sale in Billz by hand",
                order_id,
                receipt.billz_order_id,
            )
            transaction.on_commit(lambda: _alert_cancelled_sale(order_id, purchase_id, "paid_after_cancel"))
        elif not locked.events.filter(type=_E.BILLZ_SALE_RECORDED).exists():
            services.record_event(
                locked, _E.BILLZ_SALE_RECORDED, {"billz_order_id": receipt.billz_order_id}, at=now
            )
    # NEW-2: settle what Billz's stock already reflects for these products (this sale settles after the next
    # read, at the next catalog.stock_synced)
    services.settle_billz_reservations(_order_product_ids(order_id))


def _order_product_ids(order_id: int) -> list[int]:
    return list(
        StockReservation.objects.filter(order_id=order_id).values_list("product_id", flat=True).distinct()
    )


def _alert_cancelled_sale(order_id: int, purchase_id: int, case: str) -> None:
    """The ``billz.sale_of_cancelled_order`` alert (ids only; the ERROR line names the Billz sale)."""
    alerts.alert(
        "billz.sale_of_cancelled_order",
        {"order_id": order_id, "purchase_id": purchase_id, "case": case},
        scope=str(order_id),  # one alert per order: another order's sale is another action for staff
    )


@outbox.register(topics.BILLZ_PUSH_SALE)
def handle_billz_push_sale(message: OutboxMessage) -> None:
    push_sale(_payload(message))


@outbox.register(catalog_topics.STOCK_SYNCED)
def handle_stock_synced(message: OutboxMessage) -> None:
    """A Billz import read the stock again: settle the sold units it now reflects (NEW-2; idempotent)."""
    settled = services.settle_billz_reservations()
    if settled:
        logger.info("billz.reservations_settled count=%s", settled)


# A deferred sale (closed shift, decision D2) gives up after BILLZ_SHIFT_RETRY_MAX_HOURS: past that the
# nightly consistency report (K6) counts a message still deferred as stuck.
outbox.register_deferral_window(
    topics.BILLZ_PUSH_SALE, lambda: timedelta(hours=int(settings.BILLZ_SHIFT_RETRY_MAX_HOURS))
)


def on_billz_push_dead(message: OutboxMessage) -> None:
    """A sale Billz refused for good (or ten transient failures): staff record it in Billz by hand.

    The ``outbox.dead`` audit row (admin audit log) carries the reason; this ERROR line is what monitoring
    alerts on. Nothing else changes: the purchase keeps ``billz_pushed_at`` null (unpushed online sale).
    """
    payload = _payload(message)
    purchase_id = payload.get("purchase_id")
    info = loyalty_selectors.billz_push_info(int(purchase_id)) if isinstance(purchase_id, int) else None
    logger.error(
        "billz.sale_dead order_id=%s purchase_id=%s billz_order_id=%s reason=%s",
        payload.get("order_id"),
        purchase_id,
        (info.billz_sale_id if info is not None else "") or "-",  # the draft staff complete or delete
        message.last_error,
    )
    alerts.alert(
        "billz.sale_dead",
        {"order_id": payload.get("order_id"), "purchase_id": purchase_id, "message_id": message.pk},
        scope=str(payload.get("order_id")),  # one alert per order (a sale each staff enters by hand)
    )


# --- manage.py requeue_outbox of billz.push_sale (review NEW-4) ---------------------------------------------

AUDIT_PAYMENT_ATTEMPT_FORGOTTEN = "billz.payment_attempt_forgotten"
REQUEUE_WARNING = (
    "confirm the sale was not entered by hand; use billz_mark_recorded for those "
    "(a requeue would record it in Billz a second time)"
)


def _push_info(message: OutboxMessage) -> tuple[Any, Any]:
    payload = _payload(message)
    purchase_id = payload.get("purchase_id")
    info = loyalty_selectors.billz_push_info(int(purchase_id)) if isinstance(purchase_id, int) else None
    return payload.get("order_id"), info


def describe_push(message: OutboxMessage) -> str:
    """The order and the Billz draft staff look up before a replay."""
    order_id, info = _push_info(message)
    text = f"order {order_id if order_id is not None else '-'}, Billz draft "
    text += (info.billz_sale_id if info is not None else "") or "-"
    if info is not None and info.billz_payment_attempted_at is not None:
        text += f", payment attempted at {info.billz_payment_attempted_at.isoformat()}"
    return text


def push_already_recorded(message: OutboxMessage) -> str:
    """A purchase recorded in Billz meanwhile (by hand with billz_mark_recorded, or by another push)."""
    _, info = _push_info(message)
    if info is None or info.billz_pushed_at is None:
        return ""
    return f"already recorded in Billz at {info.billz_pushed_at.isoformat()} (by hand or by an earlier push)"


def push_not_recorded(message: OutboxMessage) -> str:
    """``requeue_outbox --resolved``: a dead push closes only once its sale is recorded by hand
    (``billz_mark_recorded`` takes the order off K1 and stops its units counting against stock; closing the
    message alone would not)."""
    _, info = _push_info(message)
    if info is None or info.billz_pushed_at is not None:
        return ""
    return "not recorded in Billz yet: record the hand-entered sale with manage.py billz_mark_recorded first"


def forget_payment_attempt(message: OutboxMessage, actor: Any) -> str:
    """``requeue_outbox --forget-payment-attempt``: staff confirmed in Billz that the payment never arrived;
    clear the stamp that stops a second payment (under the Order lock, audited with the staff actor)."""
    order_id, info = _push_info(message)
    if info is None or not isinstance(order_id, int):
        raise outbox.RequeueRefused(f"message #{message.pk} names no purchase")
    with transaction.atomic():
        services.lock_order(order_id, wait=services.ADMIN_LOCK_WAIT)
        _, info = _push_info(message)  # re-read under the lock
        if info is None:
            raise outbox.RequeueRefused(f"message #{message.pk} names no purchase")
        if info.billz_pushed_at is not None:
            raise outbox.RequeueRefused(f"order {order_id} is already recorded in Billz: nothing to forget")
        if info.billz_payment_attempted_at is None:
            raise outbox.RequeueRefused(f"order {order_id} has no payment attempt to forget")
        loyalty_services.clear_billz_payment_attempt(info.purchase_id, billz_sale_id=info.billz_sale_id)
        audit.record(
            action=AUDIT_PAYMENT_ATTEMPT_FORGOTTEN,
            entity_type="order",
            entity_id=str(order_id),
            actor=actor,
            before={
                "purchase_id": info.purchase_id,
                "billz_sale_id": info.billz_sale_id,
                "billz_payment_attempted_at": info.billz_payment_attempted_at.isoformat(),
            },
            after={"purchase_id": info.purchase_id, "billz_payment_attempted_at": None},
            reason="manage.py requeue_outbox --forget-payment-attempt",
        )
    logger.warning(
        "billz.payment_attempt_forgotten order_id=%s billz_order_id=%s", order_id, info.billz_sale_id or "-"
    )
    return (
        f"forgot the payment attempt of order {order_id} (Billz draft {info.billz_sale_id or '-'}): "
        "the next attempt pays the draft"
    )


outbox.on_requeue(
    topics.BILLZ_PUSH_SALE,
    outbox.RequeueHooks(
        describe=describe_push,
        skip_reason=push_already_recorded,
        warning=REQUEUE_WARNING,
        forget_payment_attempt=forget_payment_attempt,
        resolve_refusal=push_not_recorded,
    ),
)


# --- refund.submit ------------------------------------------------------------------------------------------

# The outbox lease of ``core.tasks`` (a message whose worker has not finished after it is handed to another
# worker). A refund claimed longer ago than this cannot have a reversal call in flight any more (the Click
# port bounds every call to seconds), so a redelivery may submit it again; a younger claim belongs to a
# worker that is still waiting for Click and resolves the refund itself.
RESUBMIT_AFTER = timedelta(minutes=5)


def _record(order_id: int, result: payment_services.RefundResult) -> None:
    with transaction.atomic():
        order = services.lock_order(order_id)
        services.record_refund_outcome(order, result, at=timeutils.now())


def _claim(refund_id: int, **expected: Any) -> payment_services.RefundResult | None:
    """``mark_submitting`` as a compare-and-set; ``None`` when another worker owns the refund now."""
    try:
        return payment_services.mark_submitting(refund_id, timeutils.now(), **expected)
    except payment_services.StaleRefundClaim as exc:
        logger.info("refund.submit: not claiming refund %s: %s", refund_id, exc)
        return None


def _unknown(refund_id: int, order_id: int, error: str, attempt: int) -> None:
    result = payment_services.mark_unknown(refund_id, error, timeutils.now(), expected_attempts=attempt)
    if result.changed:
        _record(order_id, result)


def _succeed(refund_id: int, order_id: int, provider_refund_id: str) -> None:
    """Click reversed the payment: ``succeeded`` (from ``submitting`` / ``unknown``)."""
    with transaction.atomic():
        order = services.lock_order(order_id)
        try:
            result = payment_services.mark_succeeded(
                refund_id, timeutils.now(), provider_refund_id=provider_refund_id
            )
        except InvalidTransition:
            # a stale worker learnt of a reversal after the refund was closed as failed: alert, never rewrite
            logger.error("refund.submit: Click reversed refund %s, but it is closed already", refund_id)
            return
        services.record_refund_outcome(order, result, at=timeutils.now())


def _resolve_after_failure(
    refund_id: int, order_id: int, payment_ref: str, port: Any, error: str, attempt: int
) -> None:
    """A transport failure after the reversal was sent: ask once more; inconclusive → ``unknown``."""
    try:
        status = port.payment_status(payment_ref)
    except ClickUnavailable as exc:
        _unknown(refund_id, order_id, f"{error}; status check: {exc}", attempt)
        return
    if status == "reversed":
        _succeed(refund_id, order_id, "")
        return
    _unknown(refund_id, order_id, f"{error}; payment still {status}", attempt)


def _reversed(port: Any, payment_ref: str) -> bool:
    try:
        return bool(port.payment_status(payment_ref) == "reversed")
    except ClickUnavailable:
        return False


PAYMENT_NOT_PAID = "payment_not_paid"


def _fail_unpaid(refund: payment_selectors.RefundRow) -> None:
    """The payment is no longer ``paid`` (a later refund returned the money): end the refund ``failed``
    ``payment_not_paid`` with its ``refund_failed`` event and never call Click (customer contract §2.13.0)."""
    with transaction.atomic():
        order = services.lock_order(refund.order_id)
        current = payment_selectors.get_refund(refund.id)
        if current is None or current.state not in (
            enums.RefundState.REQUESTED,
            enums.RefundState.SUBMITTING,
        ):
            return
        result = payment_services.mark_failed(
            refund.id, PAYMENT_NOT_PAID, "the payment is no longer paid", timeutils.now()
        )
        if result.changed:
            services.record_refund_outcome(order, result, at=timeutils.now())


def submit_refund(payload: dict[str, Any]) -> None:
    """The ``refund.submit`` effect (02 §2.2 handler; callable inline by the seeder).

    Re-checks before any port call (customer contract §2.13.0): the payment must still be ``paid`` (else the
    refund ends ``failed`` ``payment_not_paid``), and a refund whose payment has no ``click_paydoc_id`` — or
    while the Click port is fake under the production settings — becomes ``manual_required`` (its
    ``refund_requested`` event stays as written). Every Click call is keyed by the payment's
    ``click_paydoc_id`` (``RefundRow.provider_payment_ref``), never ``click_trans_id``.

    ``requested``: status check (a transport error raises, the refund stays ``requested``), claim
    (compare-and-set), reverse. ``submitting`` (a redelivery): status check first — reversed → ``succeeded``,
    check failed → ``unknown``; not reversed → re-claim only when the previous claim is older than the
    outbox lease (:data:`RESUBMIT_AFTER`), else leave it to the worker that holds it. Every outcome is
    written with the attempt this worker claimed (a newer attempt wins), and a refusal is double-checked
    against the payment status (Click refuses a duplicate of a reversal it performed).
    """
    refund_id = int(payload["refund_id"])
    refund = payment_selectors.get_refund(refund_id)
    if refund is None:
        raise LookupError(f"Refund {refund_id} does not exist")
    if refund.state not in (enums.RefundState.REQUESTED, enums.RefundState.SUBMITTING):
        return  # succeeded / failed / manual_required / unknown: nothing to do (unknown is never retried)
    if refund.provider != enums.PaymentProvider.CLICK:
        return
    payment = payment_selectors.get_payment(refund.payment_id)
    if payment is None or payment.status != enums.PaymentStatus.PAID:
        _fail_unpaid(refund)
        return
    ref = refund.provider_payment_ref
    if not ref or integration_modes.callbacks_refused("click"):
        if refund.state == enums.RefundState.REQUESTED:
            payment_services.mark_manual_required(
                refund_id,
                timeutils.now(),
                reason=enums.RefundManualReason.FAKE_MODE if ref else enums.RefundManualReason.NO_PAYDOC,
            )
        else:
            # ``submitting``: a worker may have sent the reversal before the switch (or crashed after Click
            # answered), so its outcome at Click is unknown — never left ``submitting``, which no one sees
            reason = "click fake: reversal outcome unknown" if ref else "no click_paydoc_id"
            _unknown(refund_id, refund.order_id, reason, refund.attempts)
        return
    order = Order.objects.get(pk=refund.order_id)
    port = get_click(services.fake_scenario(order, "click"))
    if refund.state == enums.RefundState.REQUESTED:
        # status check before anything is sent: a transport error here raises (the refund stays requested)
        already = port.payment_status(ref) == "reversed"
        claim = _claim(refund_id, expected_state=enums.RefundState.REQUESTED)
        if claim is None:
            return
        if already:
            _succeed(refund_id, order.pk, "")
            return
    else:
        # a redelivery: the reversal may have gone through already
        try:
            status = port.payment_status(ref)
        except ClickUnavailable as exc:
            _unknown(refund_id, order.pk, f"status check: {exc}", refund.attempts)
            return
        if status == "reversed":
            _succeed(refund_id, order.pk, "")
            return
        claim = _claim(
            refund_id,
            expected_state=enums.RefundState.SUBMITTING,
            expected_attempts=refund.attempts,
            resubmit_after=RESUBMIT_AFTER,
        )
        if claim is None:
            return
    try:
        receipt = port.refund(ref, refund.amount)
    except ClickNotProcessed as exc:
        # Click said it did not process the reversal: nothing happened, give the claim back and retry
        payment_services.release_claim(refund_id, str(exc), timeutils.now(), expected_attempts=claim.attempts)
        raise
    except ClickRefundRejected as exc:
        if _reversed(port, ref):
            _succeed(refund_id, order.pk, "")
            return
        with transaction.atomic():
            locked = services.lock_order(order.pk)
            result = payment_services.mark_failed(
                refund_id, exc.error_code, exc.message, timeutils.now(), expected_attempts=claim.attempts
            )
            if result.changed:
                services.record_refund_outcome(locked, result, at=timeutils.now())
        return
    except ClickUnavailable as exc:
        _resolve_after_failure(refund_id, order.pk, ref, port, str(exc), claim.attempts)
        return
    except BaseException as exc:
        if isinstance(exc, ClickError):  # pragma: no cover - every ClickError is handled above
            raise
        # the reversal may have left: the outcome is unknown and is never retried automatically
        _unknown_after(refund_id, order.pk, exc, claim.attempts)
        raise
    _succeed(refund_id, order.pk, receipt.provider_refund_id)


def _unknown_after(refund_id: int, order_id: int, exc: BaseException, attempt: int) -> None:
    """``unknown`` after an exception the Click port does not type (a soft time limit, a bug): the reversal
    may have reached Click. A failure to record it is logged; the original exception is what propagates."""
    logger.error("refund.submit: reversal of refund %s ended in %s", refund_id, type(exc).__name__)
    try:
        _unknown(refund_id, order_id, f"reversal outcome unknown: {type(exc).__name__}", attempt)
    except Exception:
        logger.exception("refund.submit: could not record refund %s as unknown", refund_id)


@outbox.register(topics.REFUND_SUBMIT)
def handle_refund_submit(message: OutboxMessage) -> None:
    submit_refund(_payload(message))


def on_refund_submit_dead(message: OutboxMessage) -> None:
    """Ten transport failures before submission: refund ``failed`` (``dead_letter``), never ``unknown``.

    The state is re-read under the Order → Payment locks (``expected_state="requested"``): a refund a stale
    worker moved to ``submitting`` meanwhile is left to that worker."""
    payload = _payload(message)
    refund = payment_selectors.get_refund(int(payload["refund_id"]))
    if refund is None:
        return
    with transaction.atomic():
        order = services.lock_order(refund.order_id)
        result = payment_services.mark_failed(
            refund.id,
            topics.DEAD_LETTER_ERROR_CODE,
            message.last_error,
            timeutils.now(),
            expected_state=enums.RefundState.REQUESTED,
        )
        if result.changed:
            services.record_refund_outcome(order, result, at=timeutils.now())


outbox.on_dead(topics.BTS_CREATE_SHIPMENT)(on_bts_create_dead)
outbox.on_dead(topics.REFUND_SUBMIT)(on_refund_submit_dead)
outbox.on_dead(topics.BILLZ_PUSH_SALE)(on_billz_push_dead)


# --- beat ---------------------------------------------------------------------------------------------------


@shared_task(name="commerce.expire_reservations")  # type: ignore[untyped-decorator]
def expire_reservations() -> list[int]:
    """Every minute: cancel the unpaid orders whose reservation ran out (03 §4.1 row 3)."""
    return services.expire_due_reservations()


__all__ = [
    "create_shipment",
    "expire_reservations",
    "handle_billz_push_sale",
    "handle_stock_synced",
    "describe_push",
    "forget_payment_attempt",
    "push_already_recorded",
    "handle_bts_create_shipment",
    "handle_refund_submit",
    "on_billz_push_dead",
    "on_bts_create_dead",
    "on_refund_submit_dead",
    "poll_bts",
    "push_sale",
    "report_fake_provider_open_money",
    "run_bts_poll",
    "submit_refund",
]


# --- BTS polling (customer contract §2.13.3) ---------------------------------------------------------------

POLL_LOCK_KEY = "gd:lock:poll_bts"
POLL_LOCK_SECONDS = 600
POLL_CURSOR_KEY = "poll_cursor"


@dataclass(frozen=True, slots=True)
class PollResult:
    tracked: list[int]  # order ids whose parcel was tracked and its status applied
    rejected: int
    unavailable: int
    stopped: bool
    apply_failed: int = 0  # parcels whose track or apply raised anything else (a poison parcel, skipped)


def stable_payload(parcel: Parcel) -> dict[str, Any]:
    """The stable fields of a tracked parcel (never the whole document, whose volatile fields would make every
    poll a new status): tracking number, status code and BTS's status time when it sends one."""
    payload: dict[str, Any] = {"tracking_number": parcel.tracking_number, "status": parcel.status}
    if parcel.status_at:
        payload["status_at"] = parcel.status_at
    return payload


def lock_parcel(order_id: int) -> None:
    """``pg_advisory_xact_lock`` of one order's parcel, taken by the BTS webhook and by polling around
    ``track()`` + ``apply_bts_status``: re-tracks are serialised, so a slower track can never apply a status
    older than one applied meanwhile. An advisory lock, not a row lock (no HTTP under a row lock)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"bts:{order_id}"])


# BTS statuses of a parcel that will never be delivered: deleted (1300, also a cancel's answer) and expired
# (1400). Seen on a parcel we did not cancel, staff must act (re-create it, or cancel and refund the order).
GONE_STATUSES = frozenset({"1300", "1400"})


def alert_if_gone(order_id: int, parcel: Parcel, previous: str) -> None:
    """ERROR ``bts.parcel_gone`` the first time a tracked parcel reports 1300 / 1400 (A-S8: a parcel BTS
    deleted or let expire is recorded only; the order would otherwise wait for a delivery that never
    comes)."""
    if parcel.status in GONE_STATUSES and parcel.status != previous:
        logger.error(
            "bts.parcel_gone %s",
            {"order_id": order_id, "tracking_number": parcel.tracking_number, "bts_status": parcel.status},
        )


def _track_and_apply(order_id: int, tracking_number: str, scenario: dict[str, Any]) -> Parcel:
    with transaction.atomic():
        lock_parcel(order_id)
        shipment = delivery_selectors.shipment_info(order_id)
        parcel = get_bts(scenario).track(tracking_number)
        alert_if_gone(order_id, parcel, shipment.bts_status if shipment is not None else "")
        services.apply_bts_status(
            order_id, parcel.status, enums.ShipmentVia.POLLING, payload=stable_payload(parcel)
        )
    return parcel


def run_bts_poll() -> PollResult:
    """One polling run: single flight (``cache.add`` lock), round robin after
    ``IntegrationState(bts).data["poll_cursor"]`` (written after every attempted parcel), at most
    ``BTS_POLL_BATCH`` parcels and ``BTS_POLL_MAX_SECONDS``; per-parcel failures are skipped, the run stops
    only after ``BTS_POLL_MAX_CONSECUTIVE_FAILURES`` transport failures in a row. Outcome: stopped →
    ``record_failure``; ≥ 1 tracked and applied → ``record_sync_success``; attempted but none applied (each
    parcel rejected, unavailable or failing to apply) → ``record_failure`` + ERROR
    ``bts.poll_nothing_tracked``; no candidate → nothing written. Returns at once while BTS is fake under the
    production settings."""
    empty = PollResult(tracked=[], rejected=0, unavailable=0, stopped=False)
    if integration_modes.fake_in_production("bts"):
        return empty
    if not cache.add(POLL_LOCK_KEY, 1, timeout=POLL_LOCK_SECONDS):
        return empty
    try:
        return _poll()
    finally:
        cache.delete(POLL_LOCK_KEY)


def _poll() -> PollResult:
    started = timeutils.now()
    stale_before = started - timedelta(minutes=settings.BTS_POLL_STALE_MINUTES)
    candidates = delivery_selectors.stale_shipments(stale_before)
    if not candidates:
        return PollResult(tracked=[], rejected=0, unavailable=0, stopped=False)
    state = integration_selectors.provider_state("bts")
    raw_cursor = (
        state.data.get(POLL_CURSOR_KEY) if state is not None and isinstance(state.data, dict) else None
    )
    cursor = int(raw_cursor) if isinstance(raw_cursor, int) else 0
    after = [row for row in candidates if row[0] > cursor]
    batch = (after or candidates)[: settings.BTS_POLL_BATCH]
    tracked: list[int] = []
    rejected = unavailable = apply_failed = streak = 0
    stopped = False
    for shipment_id, order_id, tracking_number in batch:
        if (timeutils.now() - started).total_seconds() >= settings.BTS_POLL_MAX_SECONDS:
            break
        order = Order.objects.get(pk=order_id)
        try:
            _track_and_apply(order_id, tracking_number, services.fake_scenario(order, "bts"))
        except BtsUnavailable:
            unavailable += 1
            streak += 1
            logger.warning("bts.poll_parcel_failed %s", {"shipment_id": shipment_id, "kind": "unavailable"})
            integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
            if streak >= settings.BTS_POLL_MAX_CONSECUTIVE_FAILURES:
                stopped = True
                break
            continue
        except BtsRejected:
            rejected += 1
            streak = 0
            logger.warning("bts.poll_parcel_failed %s", {"shipment_id": shipment_id, "kind": "rejected"})
            integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
            continue
        except Exception:  # noqa: BLE001 - a poison parcel must not hold the cursor (§2.13.3)
            apply_failed += 1
            streak = 0
            logger.exception("bts.poll_parcel_failed %s", {"shipment_id": shipment_id, "kind": "apply"})
            integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
            continue
        streak = 0
        tracked.append(order_id)
        integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
    if stopped:
        integration_services.record_failure("bts", f"poll stopped: {streak} transport failures in a row")
    elif tracked:
        integration_services.record_sync_success("bts")
    elif rejected or unavailable or apply_failed:  # attempted, nothing applied: no evidence BTS works
        integration_services.record_failure(
            "bts",
            f"poll: no parcel tracked (rejected {rejected}, unavailable {unavailable}, "
            f"apply failed {apply_failed})",
        )
        logger.error(
            "bts.poll_nothing_tracked %s",
            {"rejected": rejected, "unavailable": unavailable, "apply_failed": apply_failed},
        )
    return PollResult(
        tracked=tracked,
        rejected=rejected,
        unavailable=unavailable,
        stopped=stopped,
        apply_failed=apply_failed,
    )


@shared_task(name="commerce.poll_bts")  # type: ignore[untyped-decorator]
def poll_bts() -> list[int]:
    """BTS polling fallback (beat ``commerce-poll-bts`` every 600 s): the order ids whose parcel was
    tracked."""
    return run_bts_poll().tracked


# --- fake payment providers holding open money (customer contract §6.14)
# -------------------------------------


def report_fake_provider_open_money() -> list[str]:
    """At worker start: one ERROR ``providers.fake_with_open_money {provider, open_transactions, open_refunds,
    order_ids}`` and the alert of the same name (scope: the provider) per payment provider that is fake under
    the production settings and still holds open money (the operator procedure of §6.14 was skipped). Changes
    no payment; returns the providers reported."""
    reported = []
    for provider in (enums.PaymentProvider.CLICK, enums.PaymentProvider.PAYME):
        if not integration_modes.fake_in_production(provider):
            continue
        money = selectors.provider_open_money(provider)
        if money.open_transactions == 0 and money.open_refunds == 0:
            continue
        details = {
            "provider": str(provider),
            "open_transactions": money.open_transactions,
            "open_refunds": money.open_refunds,
            "order_ids": money.order_ids,
        }
        logger.error("providers.fake_with_open_money %s", details)
        alerts.alert("providers.fake_with_open_money", details, scope=str(provider))
        reported.append(str(provider))
    return reported


@worker_ready.connect  # type: ignore[untyped-decorator]
def _report_open_money_at_worker_start(**_kwargs: Any) -> None:
    try:
        report_fake_provider_open_money()
    except Exception:  # the check must never keep a worker from starting
        logger.exception("providers.fake_with_open_money check failed")
