"""Outbox handlers, dead-letter hooks and the beat task of `commerce` (design 03 §5, §4.1 rows 3/14/15).

Imported at startup by ``CommerceConfig.ready`` so the handlers exist in every process that dispatches the
outbox. Delivery is at-least-once: each handler is idempotent at the provider boundary.

* ``bts.create_shipment`` — under the Order lock: skip unless the order is ``processing`` and the shipment is
  not created yet; ask BTS for ``clientId = str(order_id)`` **before** creating (a redelivery after "BTS
  created the parcel, then the worker crashed" adopts it); ``shipment_created`` / ``shipment_failed``
  (source ``admin`` + actor for a manual retry). Only transport errors raise (the dispatcher retries).
* ``billz.push_sale`` — skip when the purchase is already pushed; push (Billz de-duplicates on the order
  UUID), ``loyalty.services.link_billz_sale`` and ``billz_sale_recorded`` once. A conflict raises (→ dead
  letter after the retries), never a silent success.
* ``refund.submit`` — Click: status check by ``provider_txn_id`` before any reversal; ``mark_submitting``
  is committed before the reversal call; ``succeeded`` / ``failed`` / ``unknown`` (+ events); ``unknown``
  is never retried automatically. Raises only on a transport error **before** submission, so a message that
  dies always finds the refund ``requested``.

Dead letters (``outbox.on_dead``): ``bts.create_shipment`` → shipment ``failed`` (``dead_letter``) +
``shipment_failed``; ``refund.submit`` → refund ``failed`` (``dead_letter``, never ``unknown``) +
``refund_failed``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction

from glowderma.commerce import services, topics
from glowderma.commerce.billz_sales import SaleLine, get_sale_port
from glowderma.commerce.models import Order
from glowderma.core import enums, outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts.ports import BtsRejected, CreateRequest
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click.ports import ClickRefundRejected, ClickUnavailable
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


def create_shipment(payload: dict[str, Any]) -> None:
    """The ``bts.create_shipment`` effect (callable inline by the seeder under a pinned clock)."""
    order_id = int(payload["order_id"])
    manual, actor_id = _shipment_source(payload)
    with transaction.atomic():
        order = services.lock_order(order_id)
        if (
            order.status != enums.OrderStatus.PROCESSING
            or order.fulfilment != enums.FulfilmentMethod.DELIVERY
        ):
            return  # cancelled / delivered meanwhile: nothing to create (03 §4.1 row 6)
        shipment = delivery_selectors.shipment_info(order_id)
        if shipment is None or shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return
        port = get_bts(services.fake_scenario(order, "bts"))
        now = timeutils.now()
        attempt = shipment.attempts + 1
        parcel = port.find_by_client_id(str(order_id))
        if parcel is None:
            try:
                parcel = port.create(
                    CreateRequest(
                        client_id=str(order_id),
                        attempt=attempt,
                        recipient_name=order.recipient_name,
                        recipient_phone=order.recipient_phone,
                        address=order.address_text,
                        region_code=order.address_region_code,
                        city_code=order.address_city_code,
                        comment=order.address_comment,
                    )
                )
            except BtsRejected as exc:
                number = delivery_services.mark_failed(
                    order_id, error_code=exc.error_code, error=exc.message, at=now, manual=manual
                )
                if number is not None:
                    services.record_event_once(
                        order,
                        _E.SHIPMENT_FAILED,
                        {"attempt": number, "manual": manual, "error_code": exc.error_code},
                        actor_id=actor_id,
                        at=now,
                    )
                return
        tariff = parcel.tariff if parcel.tariff is not None else order.delivery_fee
        delivery_services.mark_created(
            order_id,
            tracking_number=parcel.tracking_number,
            tariff=tariff,
            bts_order_id=parcel.bts_order_id,
            bts_status=parcel.status,
            at=now,
            manual=manual,
            payload={"tracking_number": parcel.tracking_number, "status": parcel.status},
        )
        services.record_event_once(
            order,
            _E.SHIPMENT_CREATED,
            {"tracking_number": parcel.tracking_number, "tariff": tariff, "manual": manual},
            actor_id=actor_id,
            at=now,
        )


@outbox.register(topics.BTS_CREATE_SHIPMENT)
def handle_bts_create_shipment(message: OutboxMessage) -> None:
    create_shipment(_payload(message))


def on_bts_create_dead(message: OutboxMessage) -> None:
    """Ten transport failures: the shipment is ``failed`` (``dead_letter``) and retryable by the admin."""
    payload = _payload(message)
    order_id = int(payload["order_id"])
    manual, actor_id = _shipment_source(payload)
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.status != enums.OrderStatus.PROCESSING:
            return
        number = delivery_services.mark_failed(
            order_id,
            error_code=topics.DEAD_LETTER_ERROR_CODE,
            error=message.last_error,
            at=now,
            manual=manual,
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
    """The ``billz.push_sale`` effect (callable inline by the seeder under a pinned clock)."""
    purchase_id = int(payload["purchase_id"])
    order_id = int(payload["order_id"])
    order_uuid = UUID(str(payload["billz_order_uuid"]))
    info = loyalty_selectors.billz_push_info(purchase_id)
    if info is None:
        raise LookupError(f"Purchase {purchase_id} does not exist")
    if info.billz_pushed_at is not None:
        return
    order = Order.objects.get(pk=order_id)
    lines = [
        SaleLine(billz_id=item.billz_id, quantity=item.quantity, unit_price=item.unit_price)
        for item in order.items.order_by("position")
    ]
    receipt = get_sale_port(services.fake_scenario(order, "billz")).push_sale(
        order_uuid, lines, without_cashback=True
    )
    now = timeutils.now()
    with transaction.atomic():
        locked = services.lock_order(order_id)
        loyalty_services.link_billz_sale(
            purchase_id, billz_order_uuid=order_uuid, billz_sale_id=receipt.billz_sale_id, pushed_at=now
        )
        if not locked.events.filter(type=_E.BILLZ_SALE_RECORDED).exists():
            services.record_event(locked, _E.BILLZ_SALE_RECORDED, {"billz_order_id": str(order_uuid)}, at=now)


@outbox.register(topics.BILLZ_PUSH_SALE)
def handle_billz_push_sale(message: OutboxMessage) -> None:
    push_sale(_payload(message))


# --- refund.submit ------------------------------------------------------------------------------------------


def _finish_refund(order_id: int, result: payment_services.RefundResult) -> None:
    with transaction.atomic():
        order = services.lock_order(order_id)
        services.record_refund_outcome(order, result, at=timeutils.now())


def _resolve_after_failure(
    refund_id: int, order_id: int, provider_txn_id: str, port: Any, error: str
) -> None:
    """A transport failure after the reversal was sent: ask once more; inconclusive → ``unknown``."""
    try:
        status = port.payment_status(provider_txn_id)
    except ClickUnavailable as exc:
        with transaction.atomic():
            order = services.lock_order(order_id)
            result = payment_services.mark_unknown(
                refund_id, f"{error}; status check: {exc}", timeutils.now()
            )
            services.record_refund_outcome(order, result, at=timeutils.now())
        return
    if status == "reversed":
        _succeed(refund_id, order_id, "")
        return
    with transaction.atomic():
        order = services.lock_order(order_id)
        result = payment_services.mark_unknown(refund_id, f"{error}; payment still {status}", timeutils.now())
        services.record_refund_outcome(order, result, at=timeutils.now())


def _succeed(refund_id: int, order_id: int, provider_refund_id: str) -> None:
    with transaction.atomic():
        order = services.lock_order(order_id)
        result = payment_services.mark_succeeded(
            refund_id, timeutils.now(), provider_refund_id=provider_refund_id
        )
        services.record_refund_outcome(order, result, at=timeutils.now())


def submit_refund(payload: dict[str, Any]) -> None:
    """The ``refund.submit`` effect (02 §2.2 handler; callable inline by the seeder)."""
    refund_id = int(payload["refund_id"])
    refund = payment_selectors.get_refund(refund_id)
    if refund is None:
        raise LookupError(f"Refund {refund_id} does not exist")
    if refund.state not in (enums.RefundState.REQUESTED, enums.RefundState.SUBMITTING):
        return  # succeeded / failed / manual_required / unknown: nothing to do (unknown is never retried)
    if refund.provider != enums.PaymentProvider.CLICK:
        return
    order = Order.objects.get(pk=refund.order_id)
    port = get_click(services.fake_scenario(order, "click"))
    txn = refund.provider_txn_id
    if refund.state == enums.RefundState.REQUESTED:
        # status check before anything is sent: a transport error here raises (the refund stays requested)
        if port.payment_status(txn) == "reversed":
            payment_services.mark_submitting(refund_id, timeutils.now())
            _succeed(refund_id, order.pk, "")
            return
    else:
        # a redelivery after a crash: the reversal may have gone through already
        try:
            status = port.payment_status(txn)
        except ClickUnavailable as exc:
            with transaction.atomic():
                locked = services.lock_order(order.pk)
                result = payment_services.mark_unknown(refund_id, f"status check: {exc}", timeutils.now())
                services.record_refund_outcome(locked, result, at=timeutils.now())
            return
        if status == "reversed":
            _succeed(refund_id, order.pk, "")
            return
    payment_services.mark_submitting(refund_id, timeutils.now())  # committed before the provider call
    try:
        receipt = port.refund(txn, refund.amount)
    except ClickRefundRejected as exc:
        with transaction.atomic():
            locked = services.lock_order(order.pk)
            result = payment_services.mark_failed(refund_id, exc.error_code, exc.message, timeutils.now())
            services.record_refund_outcome(locked, result, at=timeutils.now())
        return
    except ClickUnavailable as exc:
        _resolve_after_failure(refund_id, order.pk, txn, port, str(exc))
        return
    _succeed(refund_id, order.pk, receipt.provider_refund_id)


@outbox.register(topics.REFUND_SUBMIT)
def handle_refund_submit(message: OutboxMessage) -> None:
    submit_refund(_payload(message))


def on_refund_submit_dead(message: OutboxMessage) -> None:
    """Ten transport failures before submission: refund ``failed`` (``dead_letter``), never ``unknown``."""
    payload = _payload(message)
    refund = payment_selectors.get_refund(int(payload["refund_id"]))
    if refund is None or refund.state != enums.RefundState.REQUESTED:
        return
    with transaction.atomic():
        order = services.lock_order(refund.order_id)
        result = payment_services.mark_failed(
            refund.id, topics.DEAD_LETTER_ERROR_CODE, message.last_error, timeutils.now()
        )
        services.record_refund_outcome(order, result, at=timeutils.now())


outbox.on_dead(topics.BTS_CREATE_SHIPMENT)(on_bts_create_dead)
outbox.on_dead(topics.REFUND_SUBMIT)(on_refund_submit_dead)


# --- beat ---------------------------------------------------------------------------------------------------


@shared_task(name="commerce.expire_reservations")  # type: ignore[untyped-decorator]
def expire_reservations() -> list[int]:
    """Every minute: cancel the unpaid orders whose reservation ran out (03 §4.1 row 3)."""
    return services.expire_due_reservations()


__all__ = [
    "create_shipment",
    "expire_reservations",
    "handle_billz_push_sale",
    "handle_bts_create_shipment",
    "handle_refund_submit",
    "on_bts_create_dead",
    "on_refund_submit_dead",
    "push_sale",
    "submit_refund",
]
