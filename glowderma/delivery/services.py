"""Transactional writes of `delivery` (design 02 §3.1 transition table, 03 §5 ``bts.create_shipment``).

Scalar in, values out: callers pass the order id and plain values (never a ``commerce.Order``). Every write
happens while the caller (``commerce.services`` / ``commerce.tasks``) holds the **Order** lock; ``Shipment``
has no lock of its own (02 §1.9.2). The BTS adapter comes from ``integrations.bts.registry`` with the
order's fake scenario (``Order.metadata["fake"]["bts"]``), which the orchestrator hands in.

The BTS writers refuse a Yandex shipment (:class:`NotBtsShipment`): a Yandex parcel never reaches BTS, and
a BTS answer can never land on it (plan decision 3). The exception queue (``DeliveryIssue``) is written by
:func:`open_issue` / :func:`resolve_issue` / :func:`resolve_kind`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from glowderma.core import audit, enums, outbox, timeutils
from glowderma.delivery import bts, selectors
from glowderma.delivery.models import DeliveryIssue, DeliveryQuote, Shipment, ShipmentEvent
from glowderma.delivery.topics import BTS_CREATE_SHIPMENT, bts_create_shipment_key
from glowderma.integrations.bts.ports import STATUS_ACCEPTED, Parcel
from glowderma.integrations.bts.registry import get_bts


class DeliveryError(Exception):
    """Base class of the module's typed errors."""


class ShipmentMissing(DeliveryError):
    pass


class NotBtsShipment(DeliveryError):
    """A BTS operation was asked for a shipment of another carrier (a guard that must never fire)."""


class IssueNotFound(DeliveryError):
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


def _answered(shipment: Shipment, request_no: int | None) -> bool:
    """BTS already answered create request ``request_no`` (a created / create_failed event names it)."""
    if request_no is None:
        return False
    return ShipmentEvent.objects.filter(
        shipment=shipment,
        kind__in=[ShipmentEvent.Kind.CREATED, ShipmentEvent.Kind.CREATE_FAILED],
        payload__request_no=request_no,
    ).exists()


def _shipment(order_id: int) -> Shipment:
    shipment = Shipment.objects.filter(order_id=order_id).first()
    if shipment is None:
        raise ShipmentMissing(f"Order {order_id} has no shipment")
    return shipment


def _bts_shipment(order_id: int) -> Shipment:
    shipment = _shipment(order_id)
    if shipment.provider != enums.DeliveryProvider.BTS:
        raise NotBtsShipment(f"Shipment of order {order_id} is {shipment.provider}, not BTS")
    return shipment


def ensure_shipment(
    order_id: int,
    *,
    provider: str = enums.DeliveryProvider.BTS,
    method: str = "",
    quote_id: Any = None,
) -> int:
    """The delivery order's shipment row (``not_created`` at checkout); idempotent, returns its id.

    A new row takes ``provider`` / ``method`` / ``quote_id``; an existing one is returned as it is, unless
    it belongs to another carrier (``DeliveryError``: an order never changes carrier silently).
    """
    shipment, created = Shipment.objects.get_or_create(
        order_id=order_id, defaults={"provider": provider, "method": method, "quote_id": quote_id}
    )
    if not created and provider != enums.DeliveryProvider.BTS and shipment.provider != provider:
        raise DeliveryError(f"Order {order_id} ships with {shipment.provider}, not {provider}")
    return shipment.pk


def request_bts_create(order_id: int, *, manual: bool, actor_id: int | None) -> int:
    """Enqueue ``bts.create_shipment`` (automatic after payment, or a manual retry) and point the row at it.

    ``create_requests += 1`` is the ``request_no`` of the payload and of the dedupe key
    ``bts.create_shipment:{order_id}:{request_no}``; ``create_message`` derives the API's ``pending``. The
    caller has checked the preconditions (``commerce.services.retry_shipment``) under the Order lock.
    Returns the outbox message id.
    """
    with transaction.atomic():
        shipment = _bts_shipment(order_id)
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
    via: str = enums.ShipmentVia.POLLING,
    at: datetime | None = None,
    manual: bool = False,
    payload: Mapping[str, Any] | None = None,
    request_no: int | None = None,
) -> bool:
    """BTS created (or already had) the parcel: ``created``, ``via = polling`` (D-2), one ``ShipmentEvent``.

    ``tariff`` is the BTS answer, else the order's quoted fee (the caller decides, D-2). Idempotent: a row
    that is already ``created``/``cancelled`` is left alone (returns ``False``). ``request_no`` (the outbox
    payload's create request) is stored in the event payload, so a redelivered request is recognised as
    answered (:func:`glowderma.delivery.selectors.create_request_answered`).
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        shipment = _bts_shipment(order_id)
        if shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return False
        shipment.state = Shipment.State.CREATED
        shipment.tracking_number = tracking_number
        shipment.tariff = tariff
        shipment.bts_order_id = bts_order_id
        shipment.bts_status = bts_status
        shipment.via = via  # the create answer counts as a polled status by default (D-2)
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
            payload={**dict(payload or {}), **({"request_no": request_no} if request_no is not None else {})},
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
    request_no: int | None = None,
) -> int | None:
    """A definitive BTS refusal (or the dead-letter hook): ``failed``, ``attempts += 1``; returns the attempt
    number, or ``None`` when nothing was written: the parcel exists already, or create request
    ``request_no`` was answered before (a redelivery, or a second dead-letter notice of the same message).
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        shipment = _bts_shipment(order_id)
        if shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return None
        if _answered(shipment, request_no):
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
            payload={
                **({"error": error} if error else {}),
                **({"request_no": request_no} if request_no is not None else {}),
            },
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
    """A BTS webhook / poll status for a parcel (02 §3.1 ``created → created``; a cancelled parcel's late
    statuses are recorded too, the order ignores them).

    Appends a ``status_received`` event and stores ``bts_status``/``via``/``last_status_at``. A status equal
    to the newest received one (same code and payload) is a duplicate: nothing is written. The order side
    (``handed_to_courier`` / ``delivered``) is decided by ``commerce`` from ``target_status``.
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    code = str(bts_status)
    body = dict(payload or {})
    with transaction.atomic():
        shipment = _bts_shipment(order_id)
        if shipment.state not in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            raise DeliveryError(f"Shipment of order {order_id} is {shipment.state}: BTS has no parcel")
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
    ``integrations.bts.ports.BtsUnavailable`` before any write (the caller answers 503). The port answers
    ``ok`` for a parcel BTS cancelled before (idempotent at the provider boundary), so a cancel whose
    transaction rolled back after BTS agreed can simply be repeated.
    """
    _aware(at, "at")
    shipment = _bts_shipment(order_id)
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


def cancel_orphan(
    order_id: int,
    parcel: Parcel,
    *,
    scenario: Mapping[str, Any] | None = None,
    at: datetime | None = None,
    request_no: int | None = None,
) -> CancelOutcome:
    """A parcel BTS holds for an order that was cancelled while its create request was in flight.

    (BTS created it, the worker crashed before committing, the admin cancelled the order seeing no parcel.)
    The parcel is cancelled at BTS and recorded on the shipment (``created`` event + ``cancelled``), so the
    order card shows it and no courier collects it. When BTS refuses, the parcel is recorded as ``created``
    with a ``cancel_refused`` event (the caller alerts staff). A transport failure raises ``BtsUnavailable``
    before any write (the outbox retries). Returns the BTS outcome.
    """
    _aware(at, "at")
    shipment = _bts_shipment(order_id)
    if shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
        return CancelOutcome(ok=shipment.state == Shipment.State.CANCELLED)
    result = get_bts(scenario).cancel(parcel)
    instant = at or timeutils.now()
    with transaction.atomic():
        shipment.state = Shipment.State.CANCELLED if result.ok else Shipment.State.CREATED
        shipment.tracking_number = parcel.tracking_number
        shipment.tariff = parcel.tariff if parcel.tariff is not None else 0
        shipment.bts_order_id = parcel.bts_order_id
        shipment.bts_status = result.status or parcel.status or STATUS_ACCEPTED
        shipment.via = enums.ShipmentVia.POLLING
        shipment.bts_created_at = instant
        shipment.last_status_at = instant
        shipment.cancelled_at = instant if result.ok else None
        shipment.save()
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CREATED,
            bts_status=parcel.status,
            via=enums.ShipmentVia.POLLING,
            payload={
                "tracking_number": parcel.tracking_number,
                "status": parcel.status,
                "orphan": True,
                **({"request_no": request_no} if request_no is not None else {}),
            },
            at=instant,
        )
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CANCELLED if result.ok else ShipmentEvent.Kind.CANCEL_REFUSED,
            bts_status=result.status,
            payload={} if result.ok else {"reason": result.reason},
            at=instant,
        )
    return CancelOutcome(ok=result.ok, reason=result.reason)


# --- Yandex Delivery ----------------------------------------------------------------------------------------


def mark_awaiting_dispatch(order_id: int, *, at: datetime) -> bool:
    """A Yandex order was paid at ``at`` (plan §3.5): stage ``awaiting_dispatch`` and the promised window
    recomputed from ``at`` with the method's courier time (left empty when the calendar has no open day).
    Nothing goes to Yandex. Returns ``False`` when the stage had moved on already (a replayed capture).
    """
    _aware(at, "at")
    with transaction.atomic():
        shipment = _shipment(order_id)
        if shipment.provider != enums.DeliveryProvider.YANDEX:
            raise DeliveryError(f"Shipment of order {order_id} is {shipment.provider}, not Yandex")
        if shipment.stage:
            return False
        method = selectors.method_settings(shipment.method)
        window = selectors.warehouse_calendar(timeutils.local_date(at)).eta_window(
            method.eta_min_minutes, method.eta_max_minutes, at
        )
        shipment.stage = enums.ShipmentStage.AWAITING_DISPATCH
        shipment.promised_from = window.eta_from if window is not None else None
        shipment.promised_to = window.eta_to if window is not None else None
        shipment.save(update_fields=["stage", "promised_from", "promised_to"])
        return True


ISSUE_NOTE_MAX = 1000
AUDIT_ISSUE_RESOLVED = "delivery.issue_resolved"


def _clean_note(note: str) -> str:
    text = note.strip()
    if len(text) > ISSUE_NOTE_MAX:
        raise ValueError(f"note must be at most {ISSUE_NOTE_MAX} characters")
    return text


def open_issue(
    order_id: int,
    kind: str,
    *,
    code: str,
    urgent: bool,
    details: Mapping[str, Any],
    note: str = "",
    actor_id: int | None = None,
) -> tuple[int, bool]:
    """Open the ``kind`` issue of an order, or refresh the one that is open: ``(issue_id, created)``.

    Idempotent for at-least-once callers: the open row (one per order and kind) takes the new ``code``,
    ``urgent`` and ``details`` (a non-empty ``note`` too) and ``created`` is ``False``. ``details`` must be
    ids and amounts only — it reaches the alert webhook. A concurrent opener is absorbed by the partial
    unique index (the loser updates the winner's row).
    """
    if kind not in enums.DeliveryIssueKind.values:
        raise ValueError(f"Unknown delivery issue kind {kind!r}")
    text = _clean_note(note)
    body = dict(details)
    now = timeutils.now()
    for _attempt in range(2):
        with transaction.atomic():
            row = (
                DeliveryIssue.objects.select_for_update()
                .filter(order_id=order_id, kind=kind, state=enums.DeliveryIssueState.OPEN)
                .first()
            )
            if row is not None:
                row.code = code[:64]
                row.urgent = urgent
                row.details = body
                fields = ["code", "urgent", "details", "updated_at"]
                if text:
                    row.note = text
                    fields.append("note")
                row.updated_at = now
                row.save(update_fields=fields)
                return row.pk, False
            try:
                with transaction.atomic():
                    created = DeliveryIssue.objects.create(
                        order_id=order_id,
                        kind=kind,
                        code=code[:64],
                        urgent=urgent,
                        details=body,
                        note=text,
                        opened_at=now,
                        updated_at=now,
                        created_by_id=actor_id,
                    )
            except IntegrityError:
                continue  # another transaction opened it first: update that row
            return created.pk, True
    raise DeliveryError(f"Could not open the {kind} issue of order {order_id}")


def resolve_issue(issue_id: int, *, actor_id: int, note: str, request: Any = None) -> bool:
    """Staff close an issue (audited ``delivery.issue_resolved``). ``False`` when it was resolved already
    (nothing written); :class:`IssueNotFound` for an unknown id."""
    text = _clean_note(note)
    actor = get_user_model().objects.get(pk=actor_id)
    with transaction.atomic():
        row = DeliveryIssue.objects.select_for_update().filter(pk=issue_id).first()
        if row is None:
            raise IssueNotFound(f"Delivery issue {issue_id} does not exist")
        if row.state == enums.DeliveryIssueState.RESOLVED:
            return False
        now = timeutils.now()
        row.state = enums.DeliveryIssueState.RESOLVED
        row.resolved_at = now
        row.resolved_by_id = actor_id
        row.resolution_note = text
        row.updated_at = now
        row.save(update_fields=["state", "resolved_at", "resolved_by", "resolution_note", "updated_at"])
        audit.record(
            action=AUDIT_ISSUE_RESOLVED,
            entity_type="delivery_issue",
            entity_id=str(row.pk),
            actor=actor,
            request=request,
            before={"state": enums.DeliveryIssueState.OPEN},
            after={
                "state": enums.DeliveryIssueState.RESOLVED,
                "order_id": row.order_id,
                "kind": row.kind,
                "code": row.code,
                "note": text,
            },
        )
        return True


def resolve_kind(order_id: int, kind: str, note: str = "") -> bool:
    """The system closes the order's open ``kind`` issue because the case resolved itself (no actor, no
    audit); ``False`` when none was open."""
    text = _clean_note(note)
    with transaction.atomic():
        row = (
            DeliveryIssue.objects.select_for_update()
            .filter(order_id=order_id, kind=kind, state=enums.DeliveryIssueState.OPEN)
            .first()
        )
        if row is None:
            return False
        now = timeutils.now()
        row.state = enums.DeliveryIssueState.RESOLVED
        row.resolved_at = now
        row.resolution_note = text
        row.updated_at = now
        row.save(update_fields=["state", "resolved_at", "resolution_note", "updated_at"])
        return True


def purge_quotes(before: datetime) -> int:
    """Delete the quotes nobody used that expired before ``before`` (beat ``delivery.purge_quotes``);
    returns how many. A used quote stays: it documents the fee an order charged."""
    _aware(before, "before")
    deleted, _ = DeliveryQuote.objects.filter(used_at__isnull=True, expires_at__lt=before).delete()
    return deleted


__all__ = [
    "AUDIT_ISSUE_RESOLVED",
    "CancelOutcome",
    "DeliveryError",
    "IssueNotFound",
    "NotBtsShipment",
    "ShipmentMissing",
    "StatusResult",
    "apply_status",
    "cancel",
    "cancel_orphan",
    "ensure_shipment",
    "mark_created",
    "mark_awaiting_dispatch",
    "mark_failed",
    "open_issue",
    "purge_quotes",
    "request_bts_create",
    "resolve_issue",
    "resolve_kind",
]
