"""Transactional writes of `delivery` (design 02 §3.1 transition table, 03 §5 ``bts.create_shipment``).

Scalar in, values out: callers pass the order id and plain values (never a ``commerce.Order``). Every write
happens while the caller (``commerce.services`` / ``commerce.tasks``) holds the **Order** lock; ``Shipment``
has no lock of its own (02 §1.9.2). The BTS adapter comes from ``integrations.bts.registry`` with the
order's fake scenario (``Order.metadata["fake"]["bts"]``), which the orchestrator hands in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from glowderma.core import enums, outbox, timeutils
from glowderma.delivery import bts
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.delivery.topics import BTS_CREATE_SHIPMENT, bts_create_shipment_key
from glowderma.integrations.bts.ports import Parcel
from glowderma.integrations.bts.registry import get_bts


class DeliveryError(Exception):
    """Base class of the module's typed errors."""


class ShipmentMissing(DeliveryError):
    pass


@dataclass(frozen=True, slots=True)
class StatusResult:
    """What :func:`apply_status` recorded: ``changed`` False = a duplicate webhook/poll (nothing written)."""

    changed: bool
    bts_status: str
    target_status: str | None  # the OrderStatus the code maps to (02 §3.1), None = no order change


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """The synchronous BTS cancel: ``ok`` (the parcel is cancelled) or refused with BTS's ``reason``."""

    ok: bool
    reason: str = ""


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and timezone.is_naive(value):
        raise ValueError(f"{name} must be timezone-aware")


def _shipment(order_id: int) -> Shipment:
    shipment = Shipment.objects.filter(order_id=order_id).first()
    if shipment is None:
        raise ShipmentMissing(f"Order {order_id} has no shipment")
    return shipment


def ensure_shipment(order_id: int) -> int:
    """The delivery order's shipment row (``not_created`` at checkout); idempotent, returns its id."""
    shipment, _ = Shipment.objects.get_or_create(order_id=order_id)
    return shipment.pk


def request_bts_create(order_id: int, *, manual: bool, actor_id: int | None) -> int:
    """Enqueue ``bts.create_shipment`` (automatic after payment, or a manual retry) and point the row at it.

    ``create_requests += 1`` is the ``request_no`` of the payload and of the dedupe key
    ``bts.create_shipment:{order_id}:{request_no}``; ``create_message`` derives the API's ``pending``. The
    caller has checked the preconditions (``commerce.services.retry_shipment``) under the Order lock.
    Returns the outbox message id.
    """
    with transaction.atomic():
        shipment = _shipment(order_id)
        request_no = shipment.create_requests + 1
        message = outbox.enqueue(
            BTS_CREATE_SHIPMENT,
            {"order_id": order_id, "manual": manual, "request_no": request_no, "actor_id": actor_id},
            dedupe_key=bts_create_shipment_key(order_id, request_no),
        )
        shipment.create_requests = request_no
        shipment.create_message = message
        shipment.save(update_fields=["create_requests", "create_message"])
        return message.pk


def mark_created(
    order_id: int,
    *,
    tracking_number: str,
    tariff: int,
    bts_order_id: str,
    bts_status: str,
    at: datetime | None = None,
    manual: bool = False,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    """BTS created (or already had) the parcel: ``created``, ``via = polling`` (D-2), one ``ShipmentEvent``.

    ``tariff`` is the BTS answer, else the order's quoted fee (the caller decides, D-2). Idempotent: a row
    that is already ``created``/``cancelled`` is left alone (returns ``False``).
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        shipment = _shipment(order_id)
        if shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return False
        shipment.state = Shipment.State.CREATED
        shipment.tracking_number = tracking_number
        shipment.tariff = tariff
        shipment.bts_order_id = bts_order_id
        shipment.bts_status = bts_status
        shipment.via = enums.ShipmentVia.POLLING  # the create answer counts as a polled status (D-2)
        shipment.bts_created_at = instant
        shipment.last_status_at = instant
        shipment.attempts += 1
        shipment.last_attempt_at = instant
        shipment.last_error_code = ""
        shipment.last_error = ""
        shipment.save()
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CREATED,
            bts_status=bts_status,
            via=shipment.via,
            manual=manual,
            payload=dict(payload or {}),
            at=instant,
        )
        return True


def mark_failed(
    order_id: int,
    *,
    error_code: str,
    error: str = "",
    at: datetime | None = None,
    manual: bool = False,
) -> int | None:
    """A definitive BTS refusal (or the dead-letter hook): ``failed``, ``attempts += 1``; returns the attempt
    number, or ``None`` when the parcel exists already (nothing written)."""
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        shipment = _shipment(order_id)
        if shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return None
        shipment.state = Shipment.State.FAILED
        shipment.attempts += 1
        shipment.last_attempt_at = instant
        shipment.last_error_code = error_code[:32]
        shipment.last_error = error
        shipment.save()
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CREATE_FAILED,
            manual=manual,
            error_code=error_code[:32],
            payload={"error": error} if error else {},
            at=instant,
        )
        return shipment.attempts


def apply_status(
    order_id: int,
    *,
    bts_status: str,
    via: str,
    payload: Mapping[str, Any] | None = None,
    at: datetime | None = None,
) -> StatusResult:
    """A BTS webhook / poll status for a created parcel (02 §3.1 ``created → created``).

    Appends a ``status_received`` event and stores ``bts_status``/``via``/``last_status_at``. A status equal
    to the newest received one (same code and payload) is a duplicate: nothing is written. The order side
    (``handed_to_courier`` / ``delivered``) is decided by ``commerce`` from ``target_status``.
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    code = str(bts_status)
    body = dict(payload or {})
    with transaction.atomic():
        shipment = _shipment(order_id)
        if shipment.state != Shipment.State.CREATED:
            raise DeliveryError(f"Shipment of order {order_id} is {shipment.state}, not created")
        newest = (
            ShipmentEvent.objects.filter(shipment=shipment, kind=ShipmentEvent.Kind.STATUS_RECEIVED)
            .order_by("-at", "-id")
            .first()
        )
        if newest is not None and newest.bts_status == code and newest.payload == body:
            return StatusResult(changed=False, bts_status=code, target_status=bts.map_status(code))
        shipment.bts_status = code
        shipment.via = via
        shipment.last_status_at = instant
        shipment.save(update_fields=["bts_status", "via", "last_status_at"])
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.STATUS_RECEIVED,
            bts_status=code,
            via=via,
            payload=body,
            at=instant,
        )
        return StatusResult(changed=True, bts_status=code, target_status=bts.map_status(code))


def _parcel(shipment: Shipment) -> Parcel:
    return Parcel(
        client_id=str(shipment.order_id),
        tracking_number=shipment.tracking_number,
        bts_order_id=shipment.bts_order_id,
        tariff=shipment.tariff,
        status=shipment.bts_status,
    )


def cancel(
    order_id: int, *, scenario: Mapping[str, Any] | None = None, at: datetime | None = None
) -> CancelOutcome:
    """Cancel the created parcel synchronously at BTS (contract §2.2; O1: only at status 100).

    Writes only when BTS cancelled (``cancelled``, ``cancelled_at``, a ``cancelled`` event); a refusal
    returns ``ok=False`` with BTS's reason and writes nothing; a transport failure raises
    ``integrations.bts.ports.BtsUnavailable`` before any write (the caller answers 503).
    """
    _aware(at, "at")
    shipment = _shipment(order_id)
    if shipment.state != Shipment.State.CREATED:
        raise DeliveryError(f"Shipment of order {order_id} is {shipment.state}, not created")
    result = get_bts(scenario).cancel(_parcel(shipment))
    if not result.ok:
        return CancelOutcome(ok=False, reason=result.reason)
    instant = at or timeutils.now()
    with transaction.atomic():
        shipment.state = Shipment.State.CANCELLED
        shipment.cancelled_at = instant
        shipment.save(update_fields=["state", "cancelled_at"])
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CANCELLED,
            bts_status=result.status,
            at=instant,
        )
    return CancelOutcome(ok=True)


__all__ = [
    "CancelOutcome",
    "DeliveryError",
    "ShipmentMissing",
    "StatusResult",
    "apply_status",
    "cancel",
    "ensure_shipment",
    "mark_created",
    "mark_failed",
    "request_bts_create",
]
