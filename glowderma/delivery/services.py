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

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from datetime import time as time_cls
from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy

from glowderma.core import alerts, audit, enums, integration_modes, outbox, timeutils
from glowderma.delivery import bts, selectors
from glowderma.delivery import yandex as yandex_statuses
from glowderma.delivery.models import (
    PHONE_E164_REGEX,
    UZ_LATITUDE,
    UZ_LONGITUDE,
    CourierClaim,
    DeliveryIssue,
    DeliveryQuote,
    DeliverySettings,
    Shipment,
    ShipmentEvent,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.delivery.topics import BTS_CREATE_SHIPMENT, bts_create_shipment_key
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as integration_services
from glowderma.integrations.bts.ports import STATUS_ACCEPTED, Parcel
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.httpclient import safe_text
from glowderma.integrations.yandex_delivery.ports import (
    Claim,
    Point,
    YandexConflict,
    YandexError,
    YandexRejected,
    YandexUnavailable,
)
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

logger = logging.getLogger(__name__)


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


# --- Yandex Delivery: courier claims (plan §3.6 dispatch, §3.7 status sync) --------------------------------
#
# Every writer below runs while ``commerce`` holds the Order lock, and never calls Yandex: the dispatch and
# sync flows (``commerce.yandex_flow``) call the port outside any transaction and record the answers here.

_STAGE = enums.ShipmentStage
_STATUS_VIA = (enums.ShipmentVia.WEBHOOK, enums.ShipmentVia.POLLING)
# A claim snapshot as recorded by :func:`record_claim_snapshot`.
SNAPSHOT_APPLIED = "applied"
SNAPSHOT_DUPLICATE = "duplicate"  # same status, version and final price as stored
SNAPSHOT_STALE = "stale"  # older than the stored one (claims/info may lag)
SNAPSHOT_UNKNOWN_CLAIM = "unknown_claim"  # no claim of this order has that Yandex id
COST_UNKNOWN = "cost_unknown"
NO_ANSWER = "no_answer"  # an unaccepted claim kept polled whose status never moved: settled after 24 h
COURIER_DETAILS_RETENTION_DAYS = 7


class DispatchConflict(DeliveryError):
    """A claim of the shipment is still open (``courierclaim_one_open_per_shipment``): no second courier."""


@dataclass(frozen=True, slots=True)
class ClaimUpdate:
    """What :func:`record_claim_snapshot` recorded; the order side is decided by ``commerce``.

    ``live`` = the snapshot is of the shipment's current claim and that claim was open: only then do the
    stage, the state and the order move. ``picked_up`` = this claim reached pickup (now or earlier).
    """

    outcome: str
    claim_pk: int | None = None
    claim_number: int = 0
    claim_id: str = ""
    previous_status: str = ""
    status: str = ""
    known: bool = True
    live: bool = False
    current: bool = False
    stage: str = ""
    accepted_now: bool = False
    accepted: bool = False  # the claim was accepted (now or earlier)
    accept_sent: bool = False
    finished_now: bool = False
    picked_up: bool = False
    cancel_requested: bool = False
    requested_by_id: int | None = None
    tariff: int | None = None
    needs_tracking_link: bool = False
    final_price_added: int | None = None

    @property
    def applied(self) -> bool:
        return self.outcome == SNAPSHOT_APPLIED


def _yandex_shipment(order_id: int) -> Shipment:
    shipment = _shipment(order_id)
    if shipment.provider != enums.DeliveryProvider.YANDEX:
        raise DeliveryError(f"Shipment of order {order_id} is {shipment.provider}, not Yandex")
    return shipment


def _settle_cost(shipment: Shipment, paid_fee: int) -> None:
    """``cost_difference`` = the fee the customer paid − everything Yandex charged so far."""
    shipment.cost_difference = paid_fee - shipment.carrier_cost_total


def start_dispatch(
    order_id: int,
    *,
    method: str,
    taxi_class: str,
    weight_g: int,
    quoted_fee: int | None,
    estimate_price: int | None,
    approved_price: int | None,
    actor_id: int | None,
    at: datetime,
) -> int:
    """Staff verified and weighed the parcel and call a courier (plan §3.6): a new open ``CourierClaim``
    (the next ``number``, a fresh UUID ``request_id``) becomes the shipment's current claim, the parcel is
    recorded (``parcel_weight_g``, ``prepared_at`` / ``prepared_by``, ``method``), stage ``courier_requested``
    and a ``claim_requested`` event. Returns the claim pk; :class:`DispatchConflict` while another claim of
    the shipment is open (the partial unique index decides, so a double click cannot open two)."""
    _aware(at, "at")
    if method not in enums.DeliveryMethod.values:
        raise ValueError(f"Unknown delivery method {method!r}")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        number = (CourierClaim.objects.filter(shipment=shipment).aggregate(top=Max("number"))["top"] or 0) + 1
        try:
            with transaction.atomic():
                claim = CourierClaim.objects.create(
                    shipment=shipment,
                    number=number,
                    taxi_class=taxi_class,
                    method=method,
                    quoted_fee=quoted_fee,
                    estimate_price=estimate_price,
                    approved_price=approved_price,
                    requested_by_id=actor_id,
                )
        except IntegrityError as exc:
            raise DispatchConflict(f"Order {order_id} has an open courier claim") from exc
        shipment.method = method
        shipment.parcel_weight_g = weight_g
        shipment.prepared_at = at
        shipment.prepared_by_id = actor_id
        shipment.stage = _STAGE.COURIER_REQUESTED
        shipment.claim = claim
        shipment.save(
            update_fields=["method", "parcel_weight_g", "prepared_at", "prepared_by", "stage", "claim"]
        )
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CLAIM_REQUESTED,
            payload={
                "claim_number": number,
                "request_id": str(claim.request_id),
                "method": method,
                "taxi_class": taxi_class,
                "weight_g": weight_g,
                "approved_price": approved_price,
            },
            at=at,
        )
        return claim.pk


def attach_claim(claim_pk: int, claim_id: str) -> None:
    """Store Yandex's id of the claim our ``request_id`` created (idempotent; another id is an error)."""
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().get(pk=claim_pk)
        if row.claim_id == claim_id:
            return
        if row.claim_id:
            raise DeliveryError(f"Courier claim {claim_pk} is {row.claim_id}, not {claim_id}")
        row.claim_id = claim_id[:64]
        try:
            with transaction.atomic():
                row.save(update_fields=["claim_id"])
        except IntegrityError as exc:
            raise DeliveryError(f"Yandex claim {claim_id} belongs to another courier claim") from exc


def stamp_accept_sent(claim_pk: int, *, at: datetime) -> bool:
    """``accept_sent_at`` — committed **before** ``claims/accept`` is sent, so a redelivery never sends a
    second one (it re-reads the claim instead). ``False`` when the claim is closed or was stamped already."""
    _aware(at, "at")
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().get(pk=claim_pk)
        if row.ended_at is not None or row.accept_sent_at is not None:
            return False
        row.accept_sent_at = at
        row.save(update_fields=["accept_sent_at"])
        return True


def release_accept_sent(claim_pk: int) -> bool:
    """Clear ``accept_sent_at`` once Yandex provably did not take the accept (``claims/accept`` not
    processed — not sent, 401/403/408/425/429, a redirect — or refused with 409 ``old_version``): the next
    dispatch run re-checks the offer and sends one accept with the claim's fresh version. Only an open claim
    that has the stamp and was not accepted; ``False`` otherwise (nothing written)."""
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().get(pk=claim_pk)
        if row.ended_at is not None or row.accepted_at is not None or row.accept_sent_at is None:
            return False
        row.accept_sent_at = None
        row.save(update_fields=["accept_sent_at"])
        return True


def close_claim(claim_pk: int, *, at: datetime, error_code: str = "", settle: bool = True) -> bool:
    """End a claim we stop following for dispatch (refused, held for review, timed out, superseded):
    ``ended_at`` (+ ``error_code``); a claim that was never accepted costs nothing and is settled at once —
    unless ``settle=False``: Yandex may still have taken our accept, so the claim stays unsettled and the poll
    keeps reading it (an acceptance then raises ``unexpected_acceptance``). ``False`` when it was ended
    already (its ``error_code`` is then kept)."""
    _aware(at, "at")
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().get(pk=claim_pk)
        if row.ended_at is not None:
            return False
        row.ended_at = at
        fields = ["ended_at"]
        if error_code:
            row.error_code = error_code[:64]
            fields.append("error_code")
        if settle and row.accepted_at is None:
            row.settled_at = at
            fields.append("settled_at")
        row.save(update_fields=fields)
        return True


def set_dispatch_stage(
    order_id: int,
    claim_pk: int,
    *,
    stage: str,
    at: datetime,
    event_kind: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    """The dispatch of claim ``claim_pk`` ended without a courier: the shipment shows ``stage``
    (``failed``: dispatch again; ``awaiting_dispatch``: a price waits for staff) — only while that claim is
    the current one; ``failed`` before any accepted claim is also the shipment's ``failed`` state. An
    optional ``event_kind`` event (``price_review`` …) is recorded either way. Returns whether the stage
    changed."""
    _aware(at, "at")
    if stage not in enums.ShipmentStage.values:
        raise ValueError(f"Unknown shipment stage {stage!r}")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        changed = False
        if shipment.claim_id == claim_pk and shipment.stage != stage:
            shipment.stage = stage
            fields = ["stage"]
            if stage == _STAGE.FAILED and shipment.state == Shipment.State.NOT_CREATED:
                shipment.state = Shipment.State.FAILED
                fields.append("state")
            shipment.save(update_fields=fields)
            changed = True
        if event_kind is not None:
            ShipmentEvent.objects.create(
                shipment=shipment, kind=event_kind, payload=dict(payload or {}), at=at
            )
        return changed


def restore_previous_claim(order_id: int, claim_pk: int, previous_pk: int, *, at: datetime) -> None:
    """A new dispatch found the previous claim still alive at Yandex (plan §3.6 step 1): the new claim is
    closed (``previous_claim_active``), the previous one is open and current again and the stage follows its
    status — the courier it summoned is the one we follow."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        close_claim(claim_pk, at=at, error_code="previous_claim_active")
        previous = CourierClaim.objects.select_for_update().get(pk=previous_pk, shipment=shipment)
        previous.ended_at = None
        previous.settled_at = None
        previous.save(update_fields=["ended_at", "settled_at"])
        shipment.claim = previous
        shipment.stage = yandex_statuses.stage_for(previous.status) or _STAGE.FAILED
        shipment.save(update_fields=["claim", "stage"])


def set_tracking_url(claim_pk: int, url: str) -> bool:
    """The destination's tracking link of an open claim (``claims/tracking-links``; https only)."""
    if not url.startswith("https://"):
        return False
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().get(pk=claim_pk)
        if row.ended_at is not None or row.tracking_url == url[:500]:
            return False
        row.tracking_url = url[:500]
        row.save(update_fields=["tracking_url"])
        return True


def _picked_up_before(shipment: Shipment, claim: CourierClaim) -> bool:
    return ShipmentEvent.objects.filter(
        shipment=shipment,
        kind=ShipmentEvent.Kind.CLAIM_STATUS,
        carrier_status__in=list(yandex_statuses.PICKED_UP),
        payload__claim_id=claim.claim_id,
    ).exists()


def record_claim_snapshot(
    order_id: int, snapshot: Claim, *, via: str, paid_fee: int, at: datetime
) -> ClaimUpdate:
    """The delivery half of ``commerce.services.apply_yandex_claim`` (the single writer of Yandex statuses).

    The claim of this order whose id the snapshot names is updated: status (verbatim, any value), version,
    ``updated_ts``, offer, final price, courier name / vehicle, point B's id, warnings and Yandex's error
    code. A snapshot older than the stored one (``updated_ts``) is ``stale`` and one with the same status,
    version and final price a ``duplicate``: nothing but a newer ``updated_ts`` is written. A status that
    implies acceptance records it (``accepted_at``; ``accept_sent_at`` too when Yandex accepted without us,
    logged), a finished one ends the claim, and the first ``final_price`` is added once to the shipment's
    ``carrier_cost_total`` (``cost_difference`` = ``paid_fee`` − total), replacing a cancel fee counted
    before; ``cancelled_with_payment`` of our paid cancel whose fee nobody counted yet (its answer was lost)
    counts the stored ``cancel_price`` as ``cancel_fee`` once; an ended claim is settled once its
    cost is final (a price, never accepted, or a no-cost end). Only for the current, open claim do the
    shipment's stage (``delivery.yandex.stage_for``; an unknown status moves nothing) and state
    (``not_created`` / ``failed`` before any accepted claim, ``created`` after) follow. A ``claim_status``
    event with an allow-listed payload (ids, status, version, prices — never contacts) records each change.
    """
    _aware(at, "at")
    if via not in _STATUS_VIA:
        raise ValueError(f"Unknown status source {via!r}")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        claim = (
            CourierClaim.objects.select_for_update()
            .filter(shipment=shipment, claim_id=snapshot.claim_id)
            .first()
            if snapshot.claim_id
            else None
        )
        if claim is None:
            return ClaimUpdate(SNAPSHOT_UNKNOWN_CLAIM, claim_id=snapshot.claim_id)
        base: dict[str, Any] = {
            "claim_pk": claim.pk,
            "claim_number": claim.number,
            "claim_id": claim.claim_id,
        }
        if claim.updated_ts and snapshot.updated_ts and snapshot.updated_ts < claim.updated_ts:
            return ClaimUpdate(SNAPSHOT_STALE, status=claim.status, **base)
        status = snapshot.status[:40]
        if (
            status == claim.status
            and snapshot.version == claim.version
            and (snapshot.final_price is None or snapshot.final_price == claim.final_price)
        ):
            if snapshot.updated_ts and (claim.updated_ts is None or snapshot.updated_ts > claim.updated_ts):
                claim.updated_ts = snapshot.updated_ts
                claim.save(update_fields=["updated_ts"])
            return ClaimUpdate(SNAPSHOT_DUPLICATE, status=status, **base)
        previous = claim.status
        was_open = claim.ended_at is None
        current = shipment.claim_id == claim.pk
        claim.status = status
        claim.version = snapshot.version
        if snapshot.updated_ts is not None:
            claim.updated_ts = snapshot.updated_ts
        if snapshot.offer_price is not None:
            claim.offer_price = snapshot.offer_price
            claim.offer_valid_until = snapshot.offer_valid_until
        if snapshot.courier_name:
            claim.courier_name = snapshot.courier_name[:150]
        if snapshot.courier_vehicle:
            claim.courier_vehicle = snapshot.courier_vehicle[:150]
        if snapshot.destination_point_id is not None:
            claim.destination_point_id = snapshot.destination_point_id
        claim.warnings = list(snapshot.warnings)
        if snapshot.error_codes and not claim.error_code:
            claim.error_code = snapshot.error_codes[0][:64]
        known = yandex_statuses.stage_for(status) is not None
        accepted_now = False
        if claim.accepted_at is None and status in yandex_statuses.ACCEPT_IMPLIED:
            if claim.accept_sent_at is None:
                logger.warning("yandex.accepted_without_us %s", {"order_id": order_id, "claim": claim.pk})
                claim.accept_sent_at = at
            claim.accepted_at = at
            accepted_now = True
        finished_now = False
        if yandex_statuses.is_finished(status) and claim.ended_at is None:
            claim.ended_at = at
            finished_now = True
        cost_changed = False
        final_added = None
        if snapshot.final_price is not None and claim.final_price is None:
            claim.final_price = snapshot.final_price
            final_added = snapshot.final_price
            # a paid cancel's fee was counted while no final price was known: the final price replaces it
            shipment.carrier_cost_total += snapshot.final_price - (claim.cancel_fee or 0)
            cost_changed = True
        # our paid cancel (its answer lost, or not recorded in time) shows taken: its cancel-info price counts
        cost_changed = _count_quoted_cancel_fee(shipment, claim, _quoted_cancel_fee(claim)) or cost_changed
        if claim.ended_at is not None and claim.settled_at is None:
            if (
                claim.final_price is not None
                or claim.accepted_at is None
                or status in yandex_statuses.NO_COST_ENDS
            ):
                claim.settled_at = at
                cost_changed = True
        claim.save()
        live = current and was_open
        stage = shipment.stage
        if live and known:
            stage = yandex_statuses.stage_for(status) or stage
            if stage == _STAGE.RETURNING and shipment.stage == _STAGE.RETURNED:
                stage = _STAGE.RETURNED  # staff confirmed the parcel back before Yandex's lagging statuses
            shipment.stage = stage
            if stage == _STAGE.RETURNED and shipment.returned_at is None:
                shipment.returned_at = at
            if shipment.state != Shipment.State.CANCELLED:
                if claim.accepted_at is not None or shipment.state == Shipment.State.CREATED:
                    shipment.state = Shipment.State.CREATED
                elif stage == _STAGE.FAILED:
                    shipment.state = Shipment.State.FAILED
                else:
                    shipment.state = Shipment.State.NOT_CREATED
            if accepted_now and claim.offer_price is not None:
                shipment.tariff = claim.offer_price
        if cost_changed:
            _settle_cost(shipment, paid_fee)
        shipment.save()
        picked_up = (
            yandex_statuses.is_picked_up(status)
            or yandex_statuses.is_picked_up(previous)
            or _picked_up_before(shipment, claim)
        )
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CLAIM_STATUS,
            carrier_status=status,
            via=via,
            payload={
                "claim_id": claim.claim_id,
                "claim_number": claim.number,
                "status": status,
                "version": snapshot.version,
                "offer_price": snapshot.offer_price,
                "final_price": snapshot.final_price,
            },
            at=at,
        )
        return ClaimUpdate(
            SNAPSHOT_APPLIED,
            previous_status=previous,
            status=status,
            known=known,
            live=live,
            current=current,
            stage=stage,
            accepted_now=accepted_now,
            accepted=claim.accepted_at is not None,
            accept_sent=claim.accept_sent_at is not None,
            finished_now=finished_now,
            picked_up=picked_up,
            cancel_requested=shipment.cancel_requested_at is not None,
            requested_by_id=claim.requested_by_id,
            tariff=shipment.tariff,
            needs_tracking_link=(
                live
                and claim.ended_at is None
                and not claim.tracking_url
                and stage in yandex_statuses.COURIER_STAGES
            ),
            final_price_added=final_added,
            **base,
        )


def settle_cost_unknown(claim_pk: int, *, paid_fee: int, at: datetime) -> bool:
    """A finished, accepted claim Yandex gave no ``final_price`` for within 24 h: settled with error code
    ``cost_unknown`` (``cost_difference`` = paid fee − what was charged so far; staff check the cabinet).
    ``False`` when it was settled meanwhile."""
    _aware(at, "at")
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().select_related("shipment").get(pk=claim_pk)
        if row.settled_at is not None or row.ended_at is None:
            return False
        shipment = row.shipment
        # a paid cancel whose fee no writer counted (Yandex showed cancelled_with_payment before our cancel
        # was marked sent): its quoted price is what was charged, counted once now
        fee = _quoted_cancel_fee(row)
        _count_quoted_cancel_fee(shipment, row, fee)
        row.settled_at = at
        row.error_code = COST_UNKNOWN if fee is None else row.error_code
        row.save(update_fields=["settled_at", "error_code", "cancel_fee"])
        _settle_cost(shipment, paid_fee)
        shipment.save(update_fields=["carrier_cost_total", "cost_difference"])
        logger.warning("yandex.cost_unknown %s", {"order_id": shipment.order_id, "claim": row.pk})
        return True


def settle_unanswered(claim_pk: int, *, paid_fee: int, at: datetime) -> bool:
    """A claim the dispatch closed without any acceptance but left unsettled (Yandex might still have shown
    an accept we could not confirm), whose status Yandex has not moved within 24 h: it costs nothing and
    the poll stops reading it — settled with error code ``no_answer`` (nothing is added to
    ``carrier_cost_total``; ``cost_difference`` = paid fee − what was charged so far). ``False`` when it
    was settled or accepted meanwhile."""
    _aware(at, "at")
    with transaction.atomic():
        row = CourierClaim.objects.select_for_update().select_related("shipment").get(pk=claim_pk)
        if row.settled_at is not None or row.ended_at is None or row.accepted_at is not None:
            return False
        row.settled_at = at
        # the reason the claim was closed stays (cancel_requested is what the review beat's cancel_incomplete
        # reads; accept_unconfirmed / not_found / dead_letter is what staff see): no_answer only fills a blank
        row.error_code = row.error_code or NO_ANSWER
        row.save(update_fields=["settled_at", "error_code"])
        shipment = row.shipment
        _settle_cost(shipment, paid_fee)
        shipment.save(update_fields=["cost_difference"])
        logger.warning("yandex.claim_unanswered %s", {"order_id": shipment.order_id, "claim": row.pk})
        return True


def blank_courier_details(before: datetime) -> int:
    """Courier name, vehicle and tracking link of the claims that ended before ``before`` are blanked (plan
    §3.1: kept 7 days after the end); returns how many claims were cleared."""
    _aware(before, "before")
    rows = CourierClaim.objects.filter(ended_at__lt=before).exclude(
        courier_name="", courier_vehicle="", tracking_url=""
    )
    return rows.update(courier_name="", courier_vehicle="", tracking_url="", updated_at=timeutils.now())


# --- Yandex Delivery: carrier-side cancel and returns (plan §3.8) -------------------------------------------
#
# ``commerce.services.cancel_at_carrier`` calls Yandex between two short transactions and records here, under
# the Order lock; nothing below calls Yandex.

# The error code of a claim the cancel closed before any accept was sent (it summoned nobody).
CANCEL_WITHDRAWN = "cancel_requested"
# The statuses our own ``claims/cancel`` produces (free → ``cancelled``, paid → ``cancelled_with_payment``).
OUR_CANCEL_STATUSES: frozenset[str] = frozenset({"cancelled", "cancelled_with_payment"})
# A paid cancel: the ``cancel-info`` terms it is sent with, and the status Yandex then shows.
PAID_CANCEL = "paid"
PAID_CANCEL_STATUS = "cancelled_with_payment"


def request_carrier_cancel(order_id: int, *, at: datetime) -> bool:
    """Txn A of the carrier-side cancel: ``cancel_requested_at`` — committed before any Yandex call, so no
    dispatch sends an ``accept`` from here on and the status writer reads a cancel / return as ours. A stamp
    already set keeps its time (a repeated cancel resumes the first: the review beat counts from it).
    Returns whether it was set now."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        if shipment.cancel_requested_at is not None:
            return False
        shipment.cancel_requested_at = at
        shipment.save(update_fields=["cancel_requested_at"])
        return True


def clear_carrier_cancel(order_id: int) -> bool:
    """Yandex refused (or did not answer) the cancel / return: the stamp goes, the order carries on as it
    was. ``False`` when there was none."""
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        if shipment.cancel_requested_at is None:
            return False
        shipment.cancel_requested_at = None
        shipment.save(update_fields=["cancel_requested_at"])
        return True


def withdraw_claim(order_id: int, claim_pk: int, *, at: datetime) -> bool:
    """The cancel closes an open claim whose ``accept`` was never sent (it summons nobody; ``accept`` is
    the only call that does): ended with :data:`CANCEL_WITHDRAWN` (settled at once, cost 0), stage
    ``cancelled`` while it is the current claim. ``False`` when it had ended already."""
    _aware(at, "at")
    with transaction.atomic():
        closed = close_claim(claim_pk, at=at, error_code=CANCEL_WITHDRAWN)
        set_dispatch_stage(order_id, claim_pk, stage=_STAGE.CANCELLED, at=at)
        return closed


def record_cancel_fee(order_id: int, claim_pk: int, fee: int, *, paid_fee: int) -> bool:
    """The fee of a paid cancellation (``claims/cancel-info`` price) on the claim (``cancel_fee``) and —
    unless Yandex's ``final_price`` of that claim is counted already — in ``carrier_cost_total``; a
    ``final_price`` that arrives later replaces it (:func:`record_claim_snapshot` adds the difference), so a
    cost is never counted twice. ``False`` when the fee was recorded before."""
    if fee < 0:
        raise ValueError("fee must be ≥ 0")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        claim = CourierClaim.objects.select_for_update().get(pk=claim_pk, shipment=shipment)
        if claim.cancel_fee is not None:
            return False
        claim.cancel_fee = fee
        claim.save(update_fields=["cancel_fee"])
        if claim.final_price is None:
            shipment.carrier_cost_total += fee
            _settle_cost(shipment, paid_fee)
            shipment.save(update_fields=["carrier_cost_total", "cost_difference"])
        return True


def _quoted_cancel_fee(claim: CourierClaim, quoted: int | None = None) -> int | None:
    """The fee of a paid cancel still to count: Yandex shows the claim ``cancelled_with_payment`` and no
    cancel fee is recorded — ``quoted`` (a ``cancel-info`` price the caller read), else the ``cancel_price``
    of the paid cancel we sent. ``None`` otherwise."""
    if claim.cancel_fee is not None or claim.status != PAID_CANCEL_STATUS:
        return None
    if quoted is not None:
        return quoted
    return claim.cancel_price if claim.cancel_state == PAID_CANCEL else None


def _count_quoted_cancel_fee(shipment: Shipment, claim: CourierClaim, fee: int | None) -> bool:
    """``fee`` becomes the claim's ``cancel_fee`` and — unless its ``final_price`` is counted already — goes
    into ``carrier_cost_total`` (a later final price replaces it). The caller saves both rows; returns whether
    the shipment's cost changed."""
    if fee is None:
        return False
    claim.cancel_fee = fee
    if claim.final_price is not None:
        return False
    shipment.carrier_cost_total += fee
    return True


def count_cancel_fee(order_id: int, claim_pk: int, *, paid_fee: int, quoted: int | None = None) -> int | None:
    """Txn B of a cancel Yandex shows ``cancelled_with_payment``: the fee of the paid cancel no writer counted
    yet (its status was recorded before our cancel was marked sent, or another cancel took the claim while
    ours was refused) — ``quoted``, else the claim's ``cancel_price`` — becomes its ``cancel_fee`` and is
    counted once (a later ``final_price`` replaces it). The caller holds the Order lock. Returns the claim's
    cancel fee (counted now or before), ``None`` when it has none."""
    if quoted is not None and quoted < 0:
        raise ValueError("quoted must be ≥ 0")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        claim = CourierClaim.objects.select_for_update().get(pk=claim_pk, shipment=shipment)
        fee = _quoted_cancel_fee(claim, quoted)
        if fee is not None:
            if _count_quoted_cancel_fee(shipment, claim, fee):
                _settle_cost(shipment, paid_fee)
                shipment.save(update_fields=["carrier_cost_total", "cost_difference"])
            claim.save(update_fields=["cancel_fee"])
        return claim.cancel_fee


def record_cancel_sent(
    order_id: int, claim_pk: int, *, cancel_state: str, at: datetime, price: int | None = None
) -> bool:
    """Our ``claims/cancel`` of the claim was sent — Yandex answered it, or its outcome is unknown
    (``cancel_sent_at`` + the terms it was sent with + ``cancel_price``, the ``cancel-info`` price of a paid
    one, which :func:`record_claim_snapshot` counts as the fee once Yandex shows ``cancelled_with_payment``):
    from now on the cancel only re-reads the claim, it never sends a second ``claims/cancel`` /
    ``cancel-info``. ``False`` when it was recorded before (the first time, terms and price are kept).

    It writes the claim's own columns only, under the claim row lock every claim writer takes: the caller
    holds the Order lock — or, when the order stays busy past ``commerce``'s bounded wait, records the marker
    without it rather than lose it."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        claim = CourierClaim.objects.select_for_update().get(pk=claim_pk, shipment=shipment)
        if claim.cancel_sent_at is not None:
            return False
        claim.cancel_sent_at = at
        claim.cancel_state = cancel_state[:16]
        paid = cancel_state == PAID_CANCEL and price is not None and price >= 0
        claim.cancel_price = price if paid else None
        claim.save(update_fields=["cancel_sent_at", "cancel_state", "cancel_price"])
        return True


def record_return_request(order_id: int, claim_pk: int, *, at: datetime) -> bool:
    """Our ``claims/return`` of the claim was sent — Yandex answered it, or its outcome is unknown (a
    ``return_requested`` event: ids only; ``selectors.return_requested`` reads it, so the return is never
    sent a second time). ``False`` when the claim has one already. Insert-only (it locks no row), so
    ``commerce`` may record it without the Order lock when the order stays busy past its bounded wait."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        claim = CourierClaim.objects.get(pk=claim_pk, shipment=shipment)
        if selectors.return_requested(shipment.pk, claim.claim_id):
            return False
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.RETURN_REQUESTED,
            payload={"claim_id": claim.claim_id, "claim_number": claim.number},
            at=at,
        )
        return True


def confirm_return(order_id: int, *, actor_id: int, at: datetime, clear_cancel_request: bool) -> bool:
    """Staff confirm the parcel is physically back (``POST orders/{id}/confirm-return/``) — the parcel of
    the claim that carried it (``selectors.return_claim``; a later re-dispatch that summoned nobody never
    did): stage ``returned`` (if Yandex has not said so yet), ``returned_at``, ``return_confirmed_by`` and a
    ``returned`` event of that claim; ``clear_cancel_request`` ends a cancel stamp of an order that stays
    open (staff dispatch again or cancel). ``False`` when the parcel is not returning / returned, no claim
    ever took it, or its return was confirmed already."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = _yandex_shipment(order_id)
        if shipment.stage not in yandex_statuses.RETURN_STAGES:
            return False
        carried = selectors.return_claim(shipment.pk)
        if carried is None or carried.confirmed:
            return False
        claim = CourierClaim.objects.get(pk=carried.id, shipment=shipment)
        stale = (
            shipment.returned_at is None
            or shipment.stage != _STAGE.RETURNED
            or (claim.accepted_at is not None and shipment.returned_at < claim.accepted_at)
        )
        if stale:  # Yandex has not said "returned" for this claim: the parcel came back now
            shipment.returned_at = at
        shipment.stage = _STAGE.RETURNED
        shipment.return_confirmed_by_id = actor_id
        fields = ["stage", "returned_at", "return_confirmed_by"]
        if clear_cancel_request and shipment.cancel_requested_at is not None:
            shipment.cancel_requested_at = None
            fields.append("cancel_requested_at")
        shipment.save(update_fields=fields)
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.RETURNED,
            carrier_status=claim.status,
            payload={"claim_id": claim.claim_id, "claim_number": claim.number, "confirmed_by": actor_id},
            at=at,
        )
        return True


def mark_yandex_cancelled(order_id: int, *, at: datetime) -> bool:
    """``cancel_order`` of a Yandex order: ``True`` when its courier claim was cancelled **at Yandex by our
    request** (the current claim ended ``cancelled`` / ``cancelled_with_payment`` while
    ``cancel_requested_at`` is set — the event's ``bts_cancelled``); an accepted shipment (state ``created``)
    then becomes ``cancelled``. Anything else changes nothing."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = (
            Shipment.objects.select_related("claim")
            .filter(order_id=order_id, provider=enums.DeliveryProvider.YANDEX)
            .first()
        )
        if shipment is None or shipment.claim is None or shipment.cancel_requested_at is None:
            return False
        claim = shipment.claim
        if not claim.claim_id or claim.ended_at is None or claim.status not in OUR_CANCEL_STATUSES:
            return False
        if shipment.state == Shipment.State.CREATED:
            shipment.state = Shipment.State.CANCELLED
            shipment.cancelled_at = at
            shipment.save(update_fields=["state", "cancelled_at"])
        return True


def mark_delivered_by_staff(order_id: int, *, actor_id: int, at: datetime) -> bool:
    """Staff confirmed the hand-over of a parcel Yandex lost track of (the manual ``delivered`` of an
    unaccounted parcel; the caller holds the Order lock): the Yandex shipment's stage becomes ``delivered``
    and a manual ``status_received`` event (ids only) records who said so. State, costs and the claim stay
    as Yandex left them. ``False`` for a BTS shipment or one at ``delivered`` already."""
    _aware(at, "at")
    with transaction.atomic():
        shipment = (
            Shipment.objects.select_related("claim")
            .filter(order_id=order_id, provider=enums.DeliveryProvider.YANDEX)
            .first()
        )
        if shipment is None or shipment.stage == _STAGE.DELIVERED:
            return False
        shipment.stage = _STAGE.DELIVERED
        shipment.save(update_fields=["stage"])
        claim = shipment.claim
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.STATUS_RECEIVED,
            manual=True,
            payload={
                "stage": _STAGE.DELIVERED,
                "claim_id": claim.claim_id if claim is not None else "",
                "claim_number": claim.number if claim is not None else None,
                "confirmed_by": actor_id,
            },
            at=at,
        )
        return True


ISSUE_NOTE_MAX = 1000
AUDIT_ISSUE_RESOLVED = "delivery.issue_resolved"
# Alert events of the exception queue (the Telegram bot renders both: ``bot/src/glowderma_bot/messages.py``).
ALERT_ISSUE_OPENED = "delivery.issue_opened"
ALERT_ISSUE_REMINDER = "delivery.issue_reminder"
# An open urgent issue nobody touched for this long is reminded (beat ``commerce.review_delivery_issues``).
REMIND_AFTER = timedelta(minutes=30)


def _clean_note(note: str) -> str:
    text = note.strip()
    if len(text) > ISSUE_NOTE_MAX:
        raise ValueError(f"note must be at most {ISSUE_NOTE_MAX} characters")
    return text


def issue_alert_fields(row: DeliveryIssue) -> dict[str, Any]:
    """The fields of an issue alert — exactly what the bot renders: ids, the kind, the code, the urgency
    (never the staff note or the details)."""
    return {
        "order_id": row.order_id,
        "kind": row.kind,
        "code": row.code,
        "urgent": row.urgent,
        "issue_id": row.pk,
    }


def _alert_urgent(row: DeliveryIssue) -> None:
    """``delivery.issue_opened`` for an issue that just became urgent (opened urgent, or escalated). The
    alert is an outbox message of the caller's transaction: it leaves only once that commits, and never
    for an issue that was rolled back."""
    alerts.alert(ALERT_ISSUE_OPENED, issue_alert_fields(row), scope=str(row.pk))


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
    ids and amounts only. A concurrent opener is absorbed by the partial unique index (the loser updates
    the winner's row).

    This is the single place issues are opened, so it is the single place that alerts: an issue that
    becomes urgent here — opened urgent, or an open one escalated — queues ``delivery.issue_opened``
    (:func:`issue_alert_fields`, scope = the issue id) in the same transaction.
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
                escalated = urgent and not row.urgent
                row.code = code[:64]
                row.urgent = urgent
                row.details = body
                fields = ["code", "urgent", "details", "updated_at"]
                if text:
                    row.note = text
                    fields.append("note")
                row.updated_at = now
                row.save(update_fields=fields)
                if escalated:
                    _alert_urgent(row)
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
            if urgent:
                _alert_urgent(created)
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


def resolve_kind(order_id: int, kind: str, note: str = "", *, code: str | None = None) -> bool:
    """The system closes the order's open ``kind`` issue because the case resolved itself (no actor, no
    audit); with ``code`` only while the open issue still has that code (another writer may have turned it
    into another case). ``False`` when none was closed."""
    text = _clean_note(note)
    with transaction.atomic():
        row = (
            DeliveryIssue.objects.select_for_update()
            .filter(order_id=order_id, kind=kind, state=enums.DeliveryIssueState.OPEN)
            .first()
        )
        if row is None or (code is not None and row.code != code):
            return False
        now = timeutils.now()
        row.state = enums.DeliveryIssueState.RESOLVED
        row.resolved_at = now
        row.resolution_note = text
        row.updated_at = now
        row.save(update_fields=["state", "resolved_at", "resolution_note", "updated_at"])
        return True


def remind_issues(*, at: datetime, after: timedelta = REMIND_AFTER) -> list[int]:
    """Remind staff of every open urgent issue nothing touched for ``after`` (neither updated nor reminded):
    ``reminded_at`` = ``at``, ``reminders`` + 1 and one ``delivery.issue_reminder`` alert each (scope = the
    issue id and the reminder number, so every reminder is its own alert whatever the dedupe window).
    Rows another transaction holds are skipped (the next run takes them). Returns the reminded ids."""
    _aware(at, "at")
    cutoff = at - after
    with transaction.atomic():
        rows = list(
            DeliveryIssue.objects.select_for_update(skip_locked=True)
            .filter(state=enums.DeliveryIssueState.OPEN, urgent=True, updated_at__lte=cutoff)
            .filter(models.Q(reminded_at__isnull=True) | models.Q(reminded_at__lte=cutoff))
            .order_by("id")
        )
        for row in rows:
            row.reminded_at = at
            row.reminders += 1
            row.save(update_fields=["reminded_at", "reminders"])
            alerts.alert(ALERT_ISSUE_REMINDER, issue_alert_fields(row), scope=f"{row.pk}:{row.reminders}")
    return [row.pk for row in rows]


# --- Yandex account health (plan §3.9): attention.yandex_failing + alert delivery.provider_failing ----------

ALERT_PROVIDER_FAILING = "delivery.provider_failing"
# This many failures in a row (IntegrationState(yandex).consecutive_failures) raise the alert, once per run.
PROVIDER_FAILING_AFTER = 3
# check-price refusals that are the account's (payment method / contract), not the address's.
PAYMENT_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "estimating.cargocorp_payment_failure",
        "estimating.payment_method_cant_order",
        "estimating.payment_method_zone_unavailable",
    }
)
_AUTH_CODES: dict[int, str] = {401: "unauthorized", 403: "forbidden"}


def yandex_account_failure(exc: YandexError) -> str | None:
    """The code of a failure that is the account's (the token: 401 ``unauthorized`` / 403 ``forbidden``;
    payment: Yandex's code), else ``None`` (a timeout, an address Yandex does not serve…)."""
    if isinstance(exc, YandexUnavailable) and exc.status in _AUTH_CODES:
        return _AUTH_CODES[exc.status]
    if isinstance(exc, YandexRejected) and exc.code in PAYMENT_FAILURE_CODES:
        return exc.code
    return None


def yandex_failure_code(exc: YandexError) -> str:
    """A short code for any Yandex failure: the account's (:func:`yandex_account_failure`), ``unavailable``
    for an outage, else Yandex's code."""
    account = yandex_account_failure(exc)
    if account is not None:
        return account
    if isinstance(exc, YandexRejected | YandexConflict):
        return safe_text(exc.code, 64) or "rejected"
    return "unavailable"


def record_yandex_failure(source: str, code: str) -> int:
    """A failed poll or an account failure of ``check_price``: ``IntegrationState(yandex)`` counts it
    (``consecutive_failures`` → ``attention.yandex_failing``); the :data:`PROVIDER_FAILING_AFTER`-th failure
    in a row alerts ``delivery.provider_failing`` ``{provider, code}`` (scope = the Tashkent day). Its own
    transaction (callers are outside one); returns the failure count."""
    state = integration_services.record_failure("yandex", f"{source}: {code}")
    count = state.consecutive_failures
    if count == PROVIDER_FAILING_AFTER:
        logger.error("yandex.provider_failing %s", {"source": source, "code": code, "failures": count})
        alerts.alert(
            ALERT_PROVIDER_FAILING,
            {"provider": "yandex", "code": code},
            scope=timeutils.local_date(timeutils.now()).isoformat(),
        )
    return count


def record_yandex_success() -> bool:
    """A Yandex call that proves the account works (a price check): the failure run, if any, ends (one
    UPDATE matching no row while healthy)."""
    return integration_services.record_success("yandex")


def purge_quotes(before: datetime) -> int:
    """Delete the quotes nobody used that expired before ``before`` (beat ``delivery.purge_quotes``);
    returns how many. A used quote stays: it documents the fee an order charged."""
    _aware(before, "before")
    deleted, _ = DeliveryQuote.objects.filter(used_at__isnull=True, expires_at__lt=before).delete()
    return deleted


def delete_unused_quotes(customer_id: int) -> int:
    """Account deletion (``customers.services.delete_account``, in its transaction): the customer's quotes
    nobody used go at once with the saved addresses whose text and pin they copy; returns how many. A used
    quote stays (the order keeps the same address snapshot)."""
    deleted, _ = DeliveryQuote.objects.filter(customer_id=customer_id, used_at__isnull=True).delete()
    return deleted


# --- ops console and admin API: settings, calendar, suspension, tariffs check, staff incidents --------------

AUDIT_SETTINGS_UPDATED = "delivery.settings_updated"
AUDIT_SUSPENSION_UPDATED = "delivery.suspension_updated"
AUDIT_HOURS_SAVED = "delivery.hours_saved"
AUDIT_HOURS_DELETED = "delivery.hours_deleted"
AUDIT_CLOSURE_SAVED = "delivery.closure_saved"
AUDIT_CLOSURE_DELETED = "delivery.closure_deleted"
AUDIT_TARIFFS_CHECKED = "delivery.tariffs_checked"
AUDIT_ISSUE_OPENED = "delivery.issue_opened"
SETTINGS_ENTITY = "delivery_settings"
INCIDENT_CODE = "staff_report"
SUSPENSION_NOTE_MAX = 300

# Every column of ``DeliverySettings`` the ops console edits (bookkeeping and the pk excluded).
SETTINGS_FIELDS: tuple[str, ...] = (
    "warehouse_name",
    "warehouse_address",
    "warehouse_latitude",
    "warehouse_longitude",
    "contact_name",
    "contact_phone",
    "contact_email",
    "courier_comment",
    "prep_minutes",
    "dispatch_cutoff_minutes",
    "returns_accepted",
    "dispatch_suspended",
    "suspended_until",
    "suspension_note",
    "standard_enabled",
    "standard_taxi_class",
    "standard_eta_min_minutes",
    "standard_eta_max_minutes",
    "express_enabled",
    "express_taxi_class",
    "express_eta_min_minutes",
    "express_eta_max_minutes",
    "default_item_weight_g",
    "markup_percent",
    "estimate_vat_percent",
    "max_auto_increase",
    "max_loss_per_shipment",
    "prep_deadline_minutes",
)
# Personal data and the full address: the audit row says that they changed, never what they are.
PRIVATE_SETTINGS_FIELDS = frozenset({"warehouse_address", "contact_name", "contact_phone", "contact_email"})
# The classes a customer method may use (``cargo`` is never offered at checkout).
METHOD_TAXI_CLASSES: tuple[str, ...] = ("courier", "express")
# Inclusive bounds of the numeric columns (the CHECK constraints of ``DeliverySettings``).
SETTINGS_RANGES: dict[str, tuple[int | Decimal, int | Decimal]] = {
    "prep_minutes": (0, 480),
    "dispatch_cutoff_minutes": (0, 720),
    "prep_deadline_minutes": (15, 2880),
    "standard_eta_min_minutes": (1, 1440),
    "standard_eta_max_minutes": (1, 1440),
    "express_eta_min_minutes": (1, 1440),
    "express_eta_max_minutes": (1, 1440),
    "default_item_weight_g": (1, 20_000),
    "markup_percent": (Decimal(0), Decimal(100)),
    "estimate_vat_percent": (Decimal(0), Decimal(100)),
    "max_auto_increase": (0, 100_000_000),
    "max_loss_per_shipment": (0, 100_000_000),
}

# Refusals shown on the ops console page (in the viewer's language; never stored or sent).
MESSAGE_LATLNG_PAIR = gettext_lazy("Kenglik va uzunlikni birga kiriting yoki ikkalasini ham bo‘sh qoldiring.")
MESSAGE_OUTSIDE_UZBEKISTAN = gettext_lazy("Nuqta O‘zbekiston hududida bo‘lishi kerak.")
MESSAGE_PHONE = gettext_lazy("Telefon raqamini +998XXXXXXXXX ko‘rinishida kiriting.")
MESSAGE_TAXI_CLASS = gettext_lazy("Yandex klassi «courier» yoki «express» bo‘lishi kerak.")
MESSAGE_RANGE = gettext_lazy("Qiymat %(low)s dan %(high)s gacha bo‘lishi kerak.")
MESSAGE_ETA_ORDER = gettext_lazy("Eng kech vaqt eng tez vaqtdan kam bo‘lmasin.")
MESSAGE_CLASS_UNCHECKED = gettext_lazy(
    "Usulni yoqishdan oldin «Yandex bilan tekshirish» amalini bajaring: ombor nuqtasida qaysi Yandex "
    "klasslari borligi hali ma’lum emas."
)
MESSAGE_CLASS_UNAVAILABLE = gettext_lazy(
    "Yandex ombor nuqtasida «%(taxi_class)s» klassini taklif qilmaydi: usulni yoqib bo‘lmaydi."
)
MESSAGE_HOURS_ORDER = gettext_lazy("Ochilish vaqti yopilish vaqtidan oldin bo‘lsin.")
MESSAGE_WEEKDAY = gettext_lazy("Hafta kunini tanlang.")
MESSAGE_WEEKDAY_TAKEN = gettext_lazy("Bu hafta kuni allaqachon kiritilgan.")
MESSAGE_CLOSURE_TIMES = gettext_lazy(
    "Ikkala vaqtni kiriting (maxsus ish vaqti) yoki ikkalasini ham bo‘sh qoldiring (yopiq kun)."
)
MESSAGE_CLOSURE_TAKEN = gettext_lazy("Bu sana allaqachon kiritilgan.")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date_cls, time_cls)):
        return value.isoformat()
    return value


def settings_snapshot(row: DeliverySettings) -> dict[str, Any]:
    """The editable columns of ``row`` (raw values: ``Decimal``, aware ``datetime``)."""
    return {name: getattr(row, name) for name in SETTINGS_FIELDS}


def normalise_phone(value: str) -> str:
    """``+998 90 123-45-67`` / ``998901234567`` → ``+998901234567``; anything else is returned stripped (the
    validation refuses it)."""
    text = re.sub(r"[\s\-()]", "", value or "")
    if re.fullmatch(r"998\d{9}", text):
        return f"+{text}"
    return text


def method_class_refusal(taxi_class: str) -> tuple[Any, dict[str, Any]] | None:
    """Why ``taxi_class`` may not back an enabled method, or ``None``. In live mode the class must be one
    Yandex offered at the warehouse in the last ``tariffs`` answer stored by «Yandex bilan tekshirish» (or
    ``manage.py yandex_delivery_check``) in live mode; the fake allows every class."""
    if integration_modes.provider_mode("yandex") != enums.IntegrationMode.LIVE:
        return None
    data = integration_selectors.provider_data("yandex")
    tariffs = data.get("tariffs")
    if data.get("tariffs_mode") != enums.IntegrationMode.LIVE or not isinstance(tariffs, list):
        return MESSAGE_CLASS_UNCHECKED, {}
    if taxi_class not in tariffs:
        return MESSAGE_CLASS_UNAVAILABLE, {"taxi_class": taxi_class}
    return None


def settings_errors(
    values: Mapping[str, Any], *, previous: Mapping[str, Any] | None = None
) -> dict[str, list[tuple[Any, dict[str, Any]]]]:
    """Validation of a complete settings image ``values`` (``{field: [(message, params)]}``, empty = valid).

    ``previous`` is the stored image: the live-mode class rule (:func:`method_class_refusal`) applies only to
    a method being switched on or moved to another class, so an unrelated save never fails on it."""
    errors: dict[str, list[tuple[Any, dict[str, Any]]]] = {}

    def add(field: str, message: Any, **params: Any) -> None:
        errors.setdefault(field, []).append((message, params))

    latitude, longitude = values.get("warehouse_latitude"), values.get("warehouse_longitude")
    if (latitude is None) != (longitude is None):
        add("warehouse_longitude" if longitude is None else "warehouse_latitude", MESSAGE_LATLNG_PAIR)
    elif latitude is not None and longitude is not None:
        if not UZ_LATITUDE[0] <= Decimal(latitude) <= UZ_LATITUDE[1]:
            add("warehouse_latitude", MESSAGE_OUTSIDE_UZBEKISTAN)
        if not UZ_LONGITUDE[0] <= Decimal(longitude) <= UZ_LONGITUDE[1]:
            add("warehouse_longitude", MESSAGE_OUTSIDE_UZBEKISTAN)
    phone = values.get("contact_phone") or ""
    if phone and not re.fullmatch(PHONE_E164_REGEX, phone):
        add("contact_phone", MESSAGE_PHONE)
    for name, (low, high) in SETTINGS_RANGES.items():
        value = values.get(name)
        if value is not None and not low <= value <= high:
            add(name, MESSAGE_RANGE, low=low, high=high)
    for method in enums.DeliveryMethod.values:
        low_name, high_name = f"{method}_eta_min_minutes", f"{method}_eta_max_minutes"
        low_value, high_value = values.get(low_name), values.get(high_name)
        if low_value is not None and high_value is not None and high_value < low_value:
            add(high_name, MESSAGE_ETA_ORDER)
        taxi_class = values.get(f"{method}_taxi_class") or ""
        if taxi_class not in METHOD_TAXI_CLASSES:
            add(f"{method}_taxi_class", MESSAGE_TAXI_CLASS)
            continue
        enabled = bool(values.get(f"{method}_enabled"))
        was_enabled = bool(previous.get(f"{method}_enabled")) if previous is not None else False
        same_class = previous is not None and previous.get(f"{method}_taxi_class") == taxi_class
        if enabled and not (was_enabled and same_class):
            refusal = method_class_refusal(taxi_class)
            if refusal is not None:
                add(f"{method}_enabled", refusal[0], **refusal[1])
    return errors


def _validation_error(errors: Mapping[str, list[tuple[Any, dict[str, Any]]]]) -> ValidationError:
    return ValidationError(
        {
            field: [ValidationError(message, params=params or None) for message, params in entries]
            for field, entries in errors.items()
        }
    )


def _settings_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for name in SETTINGS_FIELDS:
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if name in PRIVATE_SETTINGS_FIELDS:
            diff[name] = {"changed": True}
        else:
            diff[name] = {"from": _json_value(old), "to": _json_value(new)}
    return diff


def update_settings(changes: Mapping[str, Any], *, actor: Any, request: Any = None) -> DeliverySettings:
    """Apply ``changes`` (column → value, :data:`SETTINGS_FIELDS` only: ``ValueError`` otherwise) to the
    singleton under its row lock, validated by :func:`settings_errors` (``ValidationError`` with console
    messages), and audit ``delivery.settings_updated`` with the changed columns only (``{"from", "to"}``;
    the address and the contact as ``{"changed": true}``). Nothing changed → nothing written."""
    unknown = sorted(set(changes) - set(SETTINGS_FIELDS))
    if unknown:
        raise ValueError(f"Not editable delivery settings: {', '.join(unknown)}")
    with transaction.atomic():
        row = DeliverySettings.objects.select_for_update().get(pk=1)
        before = settings_snapshot(row)
        for name, value in changes.items():
            if name == "contact_phone":
                value = normalise_phone(str(value or ""))
            elif isinstance(value, str) and name not in ("standard_taxi_class", "express_taxi_class"):
                value = value.strip()
            _aware(value if isinstance(value, datetime) else None, name)
            setattr(row, name, value)
        after = settings_snapshot(row)
        errors = settings_errors(after, previous=before)
        if errors:
            raise _validation_error(errors)
        diff = _settings_diff(before, after)
        if not diff:
            return row
        row.updated_at = timeutils.now()
        row.updated_by = actor
        row.save()
        audit.record(
            action=AUDIT_SETTINGS_UPDATED,
            entity_type=SETTINGS_ENTITY,
            entity_id="1",
            actor=actor,
            request=request,
            after=diff,
        )
        return row


@dataclass(frozen=True, slots=True)
class Suspension:
    """The dispatch switch: ``suspended`` = in effect now (the flag, until ``until`` passes)."""

    suspended: bool
    until: datetime | None
    note: str
    updated_at: datetime


def suspension_of(row: DeliverySettings, now: datetime | None = None) -> Suspension:
    instant = now or timeutils.now()
    active = row.dispatch_suspended and (row.suspended_until is None or instant < row.suspended_until)
    return Suspension(
        suspended=active,
        until=row.suspended_until if active else None,
        note=row.suspension_note,
        updated_at=row.updated_at,
    )


def set_suspension(
    suspended: bool, until: datetime | None, note: str, *, actor: Any, request: Any = None
) -> Suspension:
    """Suspend (until ``until``, or until lifted) or resume courier dispatch (plan decision 13); audited
    ``delivery.suspension_updated`` with the before/after switch. Resuming clears ``until``; a past ``until``
    is a ``ValueError`` (the API refuses it first). Nothing changed → nothing written."""
    _aware(until, "until")
    text = (note or "").strip()
    if len(text) > SUSPENSION_NOTE_MAX:
        raise ValueError(f"note must be at most {SUSPENSION_NOTE_MAX} characters")
    now = timeutils.now()
    if suspended and until is not None and until <= now:
        raise ValueError("until must be in the future")
    target_until = until if suspended else None
    with transaction.atomic():
        row = DeliverySettings.objects.select_for_update().get(pk=1)
        before = {
            "suspended": row.dispatch_suspended,
            "until": _json_value(row.suspended_until),
            "note": row.suspension_note,
        }
        after = {"suspended": suspended, "until": _json_value(target_until), "note": text}
        if before == after:
            return suspension_of(row, now)
        row.dispatch_suspended = suspended
        row.suspended_until = target_until
        row.suspension_note = text
        row.updated_at = now
        row.updated_by = actor
        row.save(
            update_fields=[
                "dispatch_suspended",
                "suspended_until",
                "suspension_note",
                "updated_at",
                "updated_by",
            ]
        )
        audit.record(
            action=AUDIT_SUSPENSION_UPDATED,
            entity_type=SETTINGS_ENTITY,
            entity_id="1",
            actor=actor,
            request=request,
            before=before,
            after=after,
        )
        return suspension_of(row, now)


def _hours_image(row: WarehouseHours) -> dict[str, Any]:
    return {
        "weekday": row.weekday,
        "opens_at": _json_value(row.opens_at),
        "closes_at": _json_value(row.closes_at),
    }


def _closure_image(row: WarehouseClosure) -> dict[str, Any]:
    return {
        "date": _json_value(row.date),
        "opens_at": _json_value(row.opens_at),
        "closes_at": _json_value(row.closes_at),
        "note": row.note,
    }


def _save_unique(row: models.Model, field: str, taken: Any) -> None:
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError as exc:  # the unique weekday / date (a second tab saved it first)
        raise ValidationError({field: [ValidationError(taken)]}) from exc


def save_hours(row: WarehouseHours, *, actor: Any, request: Any = None) -> WarehouseHours:
    """Create or change one weekday's opening hours (validated; audited ``delivery.hours_saved``)."""
    errors: dict[str, list[ValidationError]] = {}
    if row.weekday is None or not 0 <= int(row.weekday) <= 6:
        errors["weekday"] = [ValidationError(MESSAGE_WEEKDAY)]
    if row.opens_at is None or row.closes_at is None or row.opens_at >= row.closes_at:
        errors["closes_at"] = [ValidationError(MESSAGE_HOURS_ORDER)]
    if errors:
        raise ValidationError(errors)
    before = None
    if not row._state.adding:
        stored = WarehouseHours.objects.filter(pk=row.pk).first()
        before = _hours_image(stored) if stored is not None else None
    with transaction.atomic():
        _save_unique(row, "weekday", MESSAGE_WEEKDAY_TAKEN)
        audit.record(
            action=AUDIT_HOURS_SAVED,
            entity_type="warehouse_hours",
            entity_id=str(row.pk),
            actor=actor,
            request=request,
            before=before,
            after=_hours_image(row),
        )
    return row


def delete_hours(row: WarehouseHours, *, actor: Any, request: Any = None) -> None:
    """Remove a weekday (closed from now on); audited ``delivery.hours_deleted``."""
    with transaction.atomic():
        image, pk = _hours_image(row), row.pk
        row.delete()
        audit.record(
            action=AUDIT_HOURS_DELETED,
            entity_type="warehouse_hours",
            entity_id=str(pk),
            actor=actor,
            request=request,
            before=image,
        )


def save_closure(row: WarehouseClosure, *, actor: Any, request: Any = None) -> WarehouseClosure:
    """Create or change a special day (closed: both times empty; else special hours); audited
    ``delivery.closure_saved``; a new row records ``created_by``."""
    opened = row.opens_at is not None or row.closes_at is not None
    if opened and (row.opens_at is None or row.closes_at is None or row.opens_at >= row.closes_at):
        raise ValidationError({"closes_at": [ValidationError(MESSAGE_CLOSURE_TIMES)]})
    row.note = (row.note or "").strip()
    before = None
    if not row._state.adding:
        stored = WarehouseClosure.objects.filter(pk=row.pk).first()
        before = _closure_image(stored) if stored is not None else None
    elif row.created_by_id is None and getattr(actor, "pk", None) is not None:
        row.created_by = actor
    with transaction.atomic():
        _save_unique(row, "date", MESSAGE_CLOSURE_TAKEN)
        audit.record(
            action=AUDIT_CLOSURE_SAVED,
            entity_type="warehouse_closure",
            entity_id=str(row.pk),
            actor=actor,
            request=request,
            before=before,
            after=_closure_image(row),
        )
    return row


def delete_closure(row: WarehouseClosure, *, actor: Any, request: Any = None) -> None:
    """Remove a special day (the weekly hours apply again); audited ``delivery.closure_deleted``."""
    with transaction.atomic():
        image, pk = _closure_image(row), row.pk
        row.delete()
        audit.record(
            action=AUDIT_CLOSURE_DELETED,
            entity_type="warehouse_closure",
            entity_id=str(pk),
            actor=actor,
            request=request,
            before=image,
        )


@dataclass(frozen=True, slots=True)
class TariffCheck:
    """What «Yandex bilan tekshirish» found: the classes offered at the warehouse, or an error code (a
    Yandex code through ``safe_text``, ``provider_unavailable``, ``not_configured``) — never provider text."""

    ok: bool
    mode: str
    tariffs: tuple[str, ...] = ()
    error_code: str = ""


TARIFFS_NOT_CONFIGURED = "not_configured"
TARIFFS_UNAVAILABLE = "provider_unavailable"


def check_yandex_tariffs(*, actor: Any = None, request: Any = None) -> TariffCheck:
    """Ask Yandex which classes the warehouse point offers (the port's ``tariffs``, read-only) and store them
    in ``IntegrationState(yandex).data`` the way ``manage.py yandex_delivery_check`` does (``tariffs``,
    ``tariffs_checked_at``, ``tariffs_mode``, ``tariffs_point``), which the method rule reads
    (:func:`method_class_refusal`). Audited ``delivery.tariffs_checked`` (the classes or the error code).

    The caller must not hold a transaction: the Yandex call runs on its own and only the bookkeeping after it
    is written in a short transaction. A failure stores nothing in the provider document."""
    mode = integration_modes.provider_mode("yandex")
    row = DeliverySettings.objects.filter(pk=1).first()
    result: TariffCheck
    if (
        row is None
        or row.warehouse_latitude is None
        or row.warehouse_longitude is None
        or not row.warehouse_address.strip()
    ):
        result = TariffCheck(ok=False, mode=mode, error_code=TARIFFS_NOT_CONFIGURED)
    else:
        point = Point(
            lon=float(row.warehouse_longitude),
            lat=float(row.warehouse_latitude),
            fullname=row.warehouse_address.strip(),
        )
        try:
            names = tuple(tariff.name for tariff in get_yandex_delivery().tariffs(point))
        except YandexRejected as exc:
            result = TariffCheck(ok=False, mode=mode, error_code=safe_text(exc.code, 64) or "rejected")
        except YandexError:
            result = TariffCheck(ok=False, mode=mode, error_code=TARIFFS_UNAVAILABLE)
        else:
            result = TariffCheck(ok=True, mode=mode, tariffs=names)
            with transaction.atomic():
                integration_services.update_provider_data(
                    "yandex",
                    {
                        "tariffs": list(names),
                        "tariffs_checked_at": timeutils.now().isoformat(),
                        "tariffs_mode": mode,
                        "tariffs_point": [point.lon, point.lat],
                    },
                )
    if result.ok:
        logger.info("yandex.tariffs_checked mode=%s classes=%s", mode, ",".join(result.tariffs) or "-")
    else:
        logger.warning("yandex.tariffs_check_failed mode=%s code=%s", mode, result.error_code)
    audit.record(
        action=AUDIT_TARIFFS_CHECKED,
        entity_type="integration",
        entity_id="yandex",
        actor=actor,
        request=request,
        after={
            "ok": result.ok,
            "mode": mode,
            "tariffs": list(result.tariffs),
            "error_code": result.error_code,
        },
    )
    return result


def open_incident(
    order_id: int, *, note: str, urgent: bool, actor_id: int, request: Any = None
) -> tuple[int, bool]:
    """Staff report a delivery incident of an order (a wrong or missing product…): the order's open
    ``incident`` issue is opened or refreshed (:func:`open_issue`, code ``staff_report``), audited
    ``delivery.issue_opened``; one that becomes urgent alerts ``delivery.issue_opened`` (:func:`open_issue`).
    ``(issue_id, created)``; an empty note is a ``ValueError``."""
    text = _clean_note(note)
    if not text:
        raise ValueError("note must not be empty")
    actor = get_user_model().objects.get(pk=actor_id)
    kind = enums.DeliveryIssueKind.INCIDENT
    with transaction.atomic():
        issue_id, created = open_issue(
            order_id,
            kind,
            code=INCIDENT_CODE,
            urgent=urgent,
            details={"order_id": order_id},
            note=text,
            actor_id=actor_id,
        )
        audit.record(
            action=AUDIT_ISSUE_OPENED,
            entity_type="delivery_issue",
            entity_id=str(issue_id),
            actor=actor,
            request=request,
            after={
                "order_id": order_id,
                "kind": kind,
                "code": INCIDENT_CODE,
                "urgent": urgent,
                "created": created,
                "note": text,
            },
        )
        # an urgent incident (new, or an open one made urgent) alerts from open_issue
    return issue_id, created


__all__ = [
    "ALERT_ISSUE_OPENED",
    "ALERT_ISSUE_REMINDER",
    "ALERT_PROVIDER_FAILING",
    "CANCEL_WITHDRAWN",
    "OUR_CANCEL_STATUSES",
    "PAID_CANCEL",
    "PAID_CANCEL_STATUS",
    "PAYMENT_FAILURE_CODES",
    "PROVIDER_FAILING_AFTER",
    "REMIND_AFTER",
    "clear_carrier_cancel",
    "confirm_return",
    "count_cancel_fee",
    "issue_alert_fields",
    "mark_delivered_by_staff",
    "mark_yandex_cancelled",
    "record_cancel_fee",
    "record_cancel_sent",
    "record_return_request",
    "record_yandex_failure",
    "record_yandex_success",
    "remind_issues",
    "request_carrier_cancel",
    "withdraw_claim",
    "yandex_account_failure",
    "yandex_failure_code",
    "COST_UNKNOWN",
    "NO_ANSWER",
    "SNAPSHOT_APPLIED",
    "SNAPSHOT_DUPLICATE",
    "SNAPSHOT_STALE",
    "SNAPSHOT_UNKNOWN_CLAIM",
    "ClaimUpdate",
    "DispatchConflict",
    "attach_claim",
    "blank_courier_details",
    "close_claim",
    "record_claim_snapshot",
    "release_accept_sent",
    "restore_previous_claim",
    "set_dispatch_stage",
    "set_tracking_url",
    "settle_cost_unknown",
    "settle_unanswered",
    "stamp_accept_sent",
    "start_dispatch",
    "AUDIT_CLOSURE_DELETED",
    "AUDIT_CLOSURE_SAVED",
    "AUDIT_HOURS_DELETED",
    "AUDIT_HOURS_SAVED",
    "AUDIT_ISSUE_OPENED",
    "AUDIT_ISSUE_RESOLVED",
    "AUDIT_SETTINGS_UPDATED",
    "AUDIT_SUSPENSION_UPDATED",
    "AUDIT_TARIFFS_CHECKED",
    "SETTINGS_FIELDS",
    "Suspension",
    "TariffCheck",
    "check_yandex_tariffs",
    "delete_closure",
    "delete_hours",
    "method_class_refusal",
    "normalise_phone",
    "open_incident",
    "save_closure",
    "save_hours",
    "set_suspension",
    "settings_errors",
    "settings_snapshot",
    "suspension_of",
    "update_settings",
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
    "delete_unused_quotes",
    "purge_quotes",
    "request_bts_create",
    "resolve_issue",
    "resolve_kind",
]
