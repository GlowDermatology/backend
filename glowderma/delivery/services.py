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
from datetime import datetime
from datetime import time as time_cls
from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy

from glowderma.core import alerts, audit, enums, integration_modes, outbox, timeutils
from glowderma.delivery import bts, selectors
from glowderma.delivery.models import (
    PHONE_E164_REGEX,
    UZ_LATITUDE,
    UZ_LONGITUDE,
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
from glowderma.integrations.yandex_delivery.ports import Point, YandexError, YandexRejected
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


# --- ops console and admin API: settings, calendar, suspension, tariffs check, staff incidents --------------

AUDIT_SETTINGS_UPDATED = "delivery.settings_updated"
AUDIT_SUSPENSION_UPDATED = "delivery.suspension_updated"
AUDIT_HOURS_SAVED = "delivery.hours_saved"
AUDIT_HOURS_DELETED = "delivery.hours_deleted"
AUDIT_CLOSURE_SAVED = "delivery.closure_saved"
AUDIT_CLOSURE_DELETED = "delivery.closure_deleted"
AUDIT_TARIFFS_CHECKED = "delivery.tariffs_checked"
AUDIT_ISSUE_OPENED = "delivery.issue_opened"
ALERT_ISSUE_OPENED = "delivery.issue_opened"
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
    ``delivery.issue_opened``; an urgent one alerts ``delivery.issue_opened`` (ids and codes only).
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
        if urgent:
            alerts.alert(
                ALERT_ISSUE_OPENED,
                {"order_id": order_id, "kind": kind, "code": INCIDENT_CODE},
                scope=str(issue_id),
            )
    return issue_id, created


__all__ = [
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
    "purge_quotes",
    "request_bts_create",
    "resolve_issue",
    "resolve_kind",
]
