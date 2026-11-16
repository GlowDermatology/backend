"""Read side of `delivery` (design 02 §3.1 API rendering, derived ``pending``; 03 §6.4 ``bts_failed``).

The contract's ``shipment`` union (``OrderSummary.shipment``):

| state | keys |
|---|---|
| ``not_required`` | — (pickup: no row) |
| ``not_created`` | ``pending`` |
| ``failed`` | ``last_attempt_at``, ``attempts``, ``pending`` |
| ``created`` | ``tracking_number``, ``tariff``, ``updated_at``, ``via`` (also a ``cancelled`` parcel, D-1) |

``pending`` is true while the newest ``bts.create_shipment`` message is ``pending``/``failed``/``in_flight``.

Every BTS read (polling candidates, webhook lookup, ``bts_failed``, K4) goes through
``Shipment.objects.bts()``: a Yandex shipment in any state is invisible to them (plan decision 3).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, TypedDict

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Case, Count, Exists, F, OuterRef, Q, QuerySet, TextField, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce

from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.consistency import Finding, finding
from glowderma.delivery import yandex as yandex_statuses
from glowderma.delivery.models import (
    CourierClaim,
    DeliveryIssue,
    DeliverySettings,
    Shipment,
    ShipmentEvent,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.delivery.warehouse import HORIZON_DAYS, WarehouseCalendar
from glowderma.integrations.bts.ports import BtsUnavailable, City, Region
from glowderma.integrations.bts.registry import get_bts

logger = logging.getLogger(__name__)

# BTS's region/city reference list (customer contract §2.6): fresh for 24 h, kept a week under this key so a
# BTS outage serves the last list instead of 503; refreshed single-flight (REGIONS_LOCK_KEY) by the first
# request that finds it stale and by the beat ``delivery.refresh_bts_regions``.
REGIONS_CACHE_KEY = "gd:bts:regions"
REGIONS_FRESH_SECONDS = 24 * 3600
REGIONS_CACHE_SECONDS = 7 * 24 * 3600
REGIONS_LOCK_KEY = "gd:bts:regions-refresh"
REGIONS_LOCK_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ShipmentInfo:
    """The internal state of one order's shipment (what ``commerce`` checks under the Order lock)."""

    order_id: int
    state: str  # internal Shipment.State (not_created|failed|created|cancelled)
    pending: bool
    tracking_number: str
    tariff: int | None
    bts_order_id: str
    bts_status: str
    attempts: int
    create_requests: int
    created_at: datetime  # the row (created at checkout, before any create request)
    provider: str = enums.DeliveryProvider.BTS  # DeliveryProvider
    stage: str = ""  # ShipmentStage ("" for BTS)
    claim_open: bool = False  # a Yandex CourierClaim of this shipment is not ended

    @property
    def is_bts(self) -> bool:
        return self.provider == enums.DeliveryProvider.BTS


def shipment_info(order_id: int) -> ShipmentInfo | None:
    """The order's shipment (``None`` for pickup orders and orders without a row); one query."""
    open_claim = CourierClaim.objects.filter(shipment=OuterRef("pk"), ended_at__isnull=True)
    row = (
        Shipment.objects.with_pending()
        .annotate(claim_open=Exists(open_claim))
        .filter(order_id=order_id)
        .first()
    )
    if row is None:
        return None
    return ShipmentInfo(
        order_id=row.order_id,
        state=row.state,
        pending=bool(row.pending),  # type: ignore[attr-defined]
        tracking_number=row.tracking_number,
        tariff=row.tariff,
        bts_order_id=row.bts_order_id,
        bts_status=row.bts_status,
        attempts=row.attempts,
        create_requests=row.create_requests,
        created_at=row.created_at,
        provider=row.provider,
        stage=row.stage,
        claim_open=bool(row.claim_open),  # type: ignore[attr-defined]
    )


def shipment_provider(order_id: int) -> str | None:
    """The order's carrier (``None`` without a shipment row: a pickup order)."""
    return Shipment.objects.filter(order_id=order_id).values_list("provider", flat=True).first()


def create_request_answered(order_id: int, request_no: int) -> bool:
    """BTS already gave a definitive answer (parcel created, or refused) to create request ``request_no``,
    or the request's message died (``dead_letter``): a redelivery of it must not ask BTS again."""
    return ShipmentEvent.objects.filter(
        shipment__order_id=order_id,
        kind__in=[ShipmentEvent.Kind.CREATED, ShipmentEvent.Kind.CREATE_FAILED],
        payload__request_no=request_no,
    ).exists()


def api_state(shipment: Shipment | None, fulfilment: str) -> str:
    """The contract ``ShipmentState`` of an order (``cancelled`` renders as ``created``, D-1)."""
    if fulfilment == enums.FulfilmentMethod.PICKUP:
        return enums.ShipmentState.NOT_REQUIRED
    if shipment is None or shipment.state == Shipment.State.NOT_CREATED:
        return enums.ShipmentState.NOT_CREATED
    if shipment.state == Shipment.State.FAILED:
        return enums.ShipmentState.FAILED
    return enums.ShipmentState.CREATED


def updated_at(shipment: Shipment) -> datetime | None:
    """``COALESCE(last_status_at, bts_created_at)`` — the created state's ``updated_at``."""
    return shipment.last_status_at or shipment.bts_created_at


def shipment_view(
    shipment: Shipment | None, fulfilment: str, *, pending: bool | None = None
) -> dict[str, Any]:
    """The contract union of one order; ``shipment`` loaded with ``create_message`` (or ``pending`` given)."""
    state = api_state(shipment, fulfilment)
    if state == enums.ShipmentState.NOT_REQUIRED:
        return {"state": state}
    if pending is None:
        pending = shipment.is_pending if shipment is not None else False
    if state == enums.ShipmentState.NOT_CREATED:
        return {"state": state, "pending": pending}
    if shipment is None:  # unreachable: api_state returned failed/created
        raise ValueError("shipment row expected")
    if state == enums.ShipmentState.FAILED:
        return {
            "state": state,
            "last_attempt_at": shipment.last_attempt_at,
            "attempts": shipment.attempts,
            "pending": pending,
        }
    return {
        "state": state,
        "tracking_number": shipment.tracking_number,
        "tariff": shipment.tariff,
        "updated_at": updated_at(shipment),
        "via": shipment.via or enums.ShipmentVia.POLLING,
    }


def shipment_for_order(order_id: int, fulfilment: str = enums.FulfilmentMethod.DELIVERY) -> dict[str, Any]:
    """The contract union of one order (one query)."""
    row = Shipment.objects.select_related("create_message").filter(order_id=order_id).first()
    return shipment_view(row, fulfilment)


def bts_failed_count() -> int:
    """Dashboard ``attention.bts_failed``: processing orders whose BTS shipment is ``failed`` (02 §3.1)."""
    return (
        Shipment.objects.bts()
        .filter(state=Shipment.State.FAILED, order__status=enums.OrderStatus.PROCESSING)
        .count()
    )


@dataclass(frozen=True, slots=True)
class RegionCatalog:
    regions: list[Region]
    updated_at: datetime | None


def cached_region_catalog() -> RegionCatalog | None:
    """The cached list, fresh or stale (``None`` when nothing is cached)."""
    cached = cache.get(REGIONS_CACHE_KEY)
    if not isinstance(cached, dict):
        return None
    regions = [
        Region(code=r["code"], name=r["name"], cities=tuple(City(**c) for c in r["cities"]))
        for r in cached["regions"]
    ]
    return RegionCatalog(regions=regions, updated_at=datetime.fromisoformat(cached["updated_at"]))


def refresh_region_catalog(stale: RegionCatalog | None = None) -> RegionCatalog | None:
    """Fetch BTS's list once (single flight across workers) and cache it; ``None`` when another worker is
    fetching it right now. A failure raises ``BtsUnavailable`` unless ``stale`` can be served (WARNING)."""
    if not cache.add(REGIONS_LOCK_KEY, 1, timeout=REGIONS_LOCK_SECONDS):
        return None
    try:
        regions = get_bts().regions()
    except BtsUnavailable:
        if stale is None:
            raise
        logger.warning("bts.regions_refresh_failed; serving the list of %s", stale.updated_at)
        return stale
    finally:
        cache.delete(REGIONS_LOCK_KEY)
    now = timeutils.now()
    cache.set(
        REGIONS_CACHE_KEY,
        {"regions": [asdict(region) for region in regions], "updated_at": now.isoformat()},
        REGIONS_CACHE_SECONDS,
    )
    return RegionCatalog(regions=list(regions), updated_at=now)


def region_catalog() -> RegionCatalog:
    """``delivery/regions/``: BTS ``regions()``, fresh for 24 h; empty with ``updated_at`` null (nothing
    cached, no BTS call) while delivery is not offered (customer contract §2.6). A stale list is refreshed by
    one request (single flight) while every other one — and that one, when BTS fails — is served the stale
    list; only a first fetch that fails, or one that another worker is still running, raises
    ``BtsUnavailable`` (the caller answers 503)."""
    if not integration_modes.delivery_available():
        return RegionCatalog(regions=[], updated_at=None)
    cached = cached_region_catalog()
    if cached is not None and cached.updated_at is not None:
        if (timeutils.now() - cached.updated_at).total_seconds() < REGIONS_FRESH_SECONDS:
            return cached
    fetched = refresh_region_catalog(stale=cached)
    if fetched is not None:
        return fetched
    if cached is not None:
        return cached
    raise BtsUnavailable("BTS's region list is being fetched by another request")


def region_known(region_code: str, city_code: str) -> bool:
    """``region_code`` / ``city_code`` are a pair of BTS's reference list (address checks, §2.6)."""
    if not region_code or not city_code:
        return False
    for region in region_catalog().regions:
        if region.code == region_code:
            return any(city.code == city_code for city in region.cities)
    return False


def stale_shipments(stale_before: datetime) -> list[tuple[int, int, str]]:
    """BTS polling candidates (customer contract §2.13.3): ``created`` shipments with a tracking number whose
    order is ``processing`` / ``handed_to_courier`` and whose ``last_status_at`` (or creation) is older than
    ``stale_before``; ``(shipment_id, order_id, tracking_number)`` in id order."""
    rows = (
        Shipment.objects.bts()
        .filter(
            state=Shipment.State.CREATED,
            order__status__in=[enums.OrderStatus.PROCESSING, enums.OrderStatus.HANDED_TO_COURIER],
        )
        .exclude(tracking_number="")
        .annotate(reference=Coalesce("last_status_at", "bts_created_at"))
        .filter(Q(reference__lt=stale_before) | Q(reference__isnull=True))
        .order_by("id")
        .values_list("id", "order_id", "tracking_number")
    )
    return [(int(pk), int(order_id), str(tracking)) for pk, order_id, tracking in rows]


def shipment_by_tracking(tracking_number: str) -> ShipmentInfo | None:
    """The BTS shipment whose stored tracking number is ``tracking_number`` (BTS webhook lookup)."""
    if not tracking_number:
        return None
    order_id = (
        Shipment.objects.bts()
        .filter(tracking_number=tracking_number)
        .values_list("order_id", flat=True)
        .first()
    )
    return None if order_id is None else shipment_info(int(order_id))


def untracked_parcels(status_before: datetime, *, limit: int) -> Finding:
    """Consistency report K4: parcels created at BTS for orders not delivered yet (``processing`` or
    ``handed_to_courier``) whose last BTS status — or their creation, before any — is older than
    ``status_before``. A status equal to the previous one writes nothing (:func:`services.apply_status`), so
    this reads "no new BTS status for N hours": a parcel stuck at BTS, or tracking (webhook and polling) that
    stopped. The ids are order ids.

    Plan (EXPLAIN on 120 000 shipments, 240 open orders): a merge join — ``order_status_idx`` on the two open
    order statuses (a few hundred rows, sorted by id) against a walk of the shipments' unique ``order_id``
    index in order, the state and the timestamps filtered on every shipment row. The planner picks it for the
    ``ORDER BY order_id LIMIT`` of :func:`~glowderma.core.consistency.finding` (an IN-subquery on the orders
    gets the same plan), so the cost grows with all shipments ever created (~45 ms per 120 000): linear, far
    inside the check's 60 s statement timeout for years of parcels."""
    queryset = (
        Shipment.objects.bts()
        .filter(
            state=Shipment.State.CREATED,
            order__status__in=[enums.OrderStatus.PROCESSING, enums.OrderStatus.HANDED_TO_COURIER],
        )
        .annotate(reference=Coalesce("last_status_at", "bts_created_at"))
        .filter(reference__lt=status_before)
    )
    return finding(queryset, limit=limit, id_field="order_id")


# --- Yandex Delivery: warehouse calendar, method settings, exception queue ----------------------------------


def warehouse_calendar(today: date | None = None) -> WarehouseCalendar:
    """The :class:`~glowderma.delivery.warehouse.WarehouseCalendar` of the stored settings, hours and the
    closures from ``today`` (Tashkent; default the current day) to the end of the search horizon. Three
    queries; reads never create the settings row (missing → defaults, not configured)."""
    settings_row = DeliverySettings.objects.filter(pk=1).first() or DeliverySettings()
    start = today or timeutils.local_date(timeutils.now())
    hours = {row.weekday: (row.opens_at, row.closes_at) for row in WarehouseHours.objects.all()}
    closures: dict[date, tuple[time, time] | None] = {}
    for row in WarehouseClosure.objects.filter(
        date__gte=start - timedelta(days=1), date__lte=start + timedelta(days=HORIZON_DAYS + 1)
    ):
        opened = row.opens_at is not None and row.closes_at is not None
        closures[row.date] = (row.opens_at, row.closes_at) if opened else None  # type: ignore[assignment]
    return WarehouseCalendar(
        hours=hours,
        closures=closures,
        prep_minutes=settings_row.prep_minutes,
        dispatch_cutoff_minutes=settings_row.dispatch_cutoff_minutes,
        returns_accepted=settings_row.returns_accepted,
        dispatch_suspended=settings_row.dispatch_suspended,
        suspended_until=settings_row.suspended_until,
        configured=warehouse_configured(settings_row),
    )


def warehouse_configured(settings_row: DeliverySettings) -> bool:
    """Point A is complete: a pin, an address and a contact name and phone (Yandex needs all four)."""
    return (
        settings_row.warehouse_latitude is not None
        and settings_row.warehouse_longitude is not None
        and bool(settings_row.warehouse_address.strip())
        and bool(settings_row.contact_name.strip())
        and bool(settings_row.contact_phone)
    )


@dataclass(frozen=True, slots=True)
class MethodSettings:
    """One delivery method as configured: switched on, its Yandex class and its courier time once called."""

    method: str  # DeliveryMethod
    enabled: bool
    taxi_class: str
    eta_min_minutes: int
    eta_max_minutes: int


def method_settings(method: str, settings_row: DeliverySettings | None = None) -> MethodSettings:
    """The configuration of ``method`` (``standard`` / ``express``); ``ValueError`` for anything else."""
    if method not in enums.DeliveryMethod.values:
        raise ValueError(f"Unknown delivery method {method!r}")
    row = settings_row or DeliverySettings.objects.filter(pk=1).first() or DeliverySettings()
    return MethodSettings(
        method=method,
        enabled=bool(getattr(row, f"{method}_enabled")),
        taxi_class=str(getattr(row, f"{method}_taxi_class")),
        eta_min_minutes=int(getattr(row, f"{method}_eta_min_minutes")),
        eta_max_minutes=int(getattr(row, f"{method}_eta_max_minutes")),
    )


@dataclass(frozen=True, slots=True)
class IssueInfo:
    """One ``DeliveryIssue`` row as the queue and the order card show it (no staff note in alerts)."""

    id: int
    order_id: int
    kind: str  # DeliveryIssueKind
    code: str
    urgent: bool
    state: str  # DeliveryIssueState
    details: dict[str, Any]
    note: str
    opened_at: datetime
    updated_at: datetime
    reminded_at: datetime | None
    reminders: int
    resolved_at: datetime | None
    resolved_by_id: int | None
    resolution_note: str
    created_by_id: int | None


def _issue_info(row: DeliveryIssue) -> IssueInfo:
    return IssueInfo(
        id=row.pk,
        order_id=row.order_id,
        kind=row.kind,
        code=row.code,
        urgent=row.urgent,
        state=row.state,
        details=dict(row.details) if isinstance(row.details, dict) else {},
        note=row.note,
        opened_at=row.opened_at,
        updated_at=row.updated_at,
        reminded_at=row.reminded_at,
        reminders=row.reminders,
        resolved_at=row.resolved_at,
        resolved_by_id=row.resolved_by_id,
        resolution_note=row.resolution_note,
        created_by_id=row.created_by_id,
    )


def issue(issue_id: int) -> IssueInfo | None:
    row = DeliveryIssue.objects.filter(pk=issue_id).first()
    return None if row is None else _issue_info(row)


def open_issues(
    *,
    order_id: int | None = None,
    kind: str | None = None,
    urgent: bool | None = None,
    limit: int | None = None,
) -> list[IssueInfo]:
    """Open issues, urgent first, then oldest first (``deliveryissue_queue_idx``)."""
    rows = DeliveryIssue.objects.filter(state=enums.DeliveryIssueState.OPEN)
    if order_id is not None:
        rows = rows.filter(order_id=order_id)
    if kind is not None:
        rows = rows.filter(kind=kind)
    if urgent is not None:
        rows = rows.filter(urgent=urgent)
    ordered = rows.order_by("-urgent", "opened_at", "id")
    return [_issue_info(row) for row in (ordered[:limit] if limit is not None else ordered)]


def has_open_issue(order_id: int, kind: str) -> bool:
    return DeliveryIssue.objects.filter(
        order_id=order_id, kind=kind, state=enums.DeliveryIssueState.OPEN
    ).exists()


def issue_counts() -> dict[str, int]:
    """``{open, urgent}`` of the queue (nav badge, dashboard); one query."""
    counts = DeliveryIssue.objects.filter(state=enums.DeliveryIssueState.OPEN).aggregate(
        open=Count("id"), urgent=Count("id", filter=Q(urgent=True))
    )
    return {"open": int(counts["open"] or 0), "urgent": int(counts["urgent"] or 0)}


def settings_row() -> DeliverySettings:
    """The stored singleton, or an unsaved default row when it is missing (reads never create it)."""
    return DeliverySettings.objects.filter(pk=1).first() or DeliverySettings()


# --- Yandex Delivery: courier claims (dispatch and status sync, plan §3.6–§3.7) -----------------------------


@dataclass(frozen=True, slots=True)
class ClaimRow:
    """One ``CourierClaim`` as the dispatch and status flows read it (no courier name or vehicle)."""

    id: int
    shipment_id: int
    order_id: int
    number: int
    request_id: str  # our UUID, the idempotency key of claims/create
    claim_id: str  # Yandex's id ("" until claims/create answered)
    status: str
    version: int | None
    updated_ts: datetime | None
    taxi_class: str
    method: str
    quoted_fee: int | None
    estimate_price: int | None
    offer_price: int | None
    offer_valid_until: datetime | None
    approved_price: int | None
    final_price: int | None
    error_code: str
    accept_sent_at: datetime | None
    accepted_at: datetime | None
    ended_at: datetime | None
    settled_at: datetime | None
    created_at: datetime
    tracking_url: str
    requested_by_id: int | None
    current: bool  # the shipment's current claim
    destination_point_id: int | None = None  # Yandex's id of point B (what claims/return needs)
    cancel_fee: int | None = None  # so'm: the fee of a paid cancellation we asked for
    cancel_sent_at: datetime | None = None  # our claims/cancel was sent (answered or outcome unknown)
    cancel_state: str = ""  # the terms it was sent with (free | paid)

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


def _claim_row(row: CourierClaim) -> ClaimRow:
    return ClaimRow(
        id=row.pk,
        shipment_id=row.shipment_id,
        order_id=row.shipment.order_id,
        number=row.number,
        request_id=str(row.request_id),
        claim_id=row.claim_id,
        status=row.status,
        version=row.version,
        updated_ts=row.updated_ts,
        taxi_class=row.taxi_class,
        method=row.method,
        quoted_fee=row.quoted_fee,
        estimate_price=row.estimate_price,
        offer_price=row.offer_price,
        offer_valid_until=row.offer_valid_until,
        approved_price=row.approved_price,
        final_price=row.final_price,
        error_code=row.error_code,
        accept_sent_at=row.accept_sent_at,
        accepted_at=row.accepted_at,
        ended_at=row.ended_at,
        settled_at=row.settled_at,
        created_at=row.created_at,
        tracking_url=row.tracking_url,
        requested_by_id=row.requested_by_id,
        current=row.shipment.claim_id == row.pk,
        destination_point_id=row.destination_point_id,
        cancel_fee=row.cancel_fee,
        cancel_sent_at=row.cancel_sent_at,
        cancel_state=row.cancel_state,
    )


def _claims() -> QuerySet[CourierClaim]:
    return CourierClaim.objects.select_related("shipment")


def claim_row(claim_pk: int) -> ClaimRow | None:
    row = _claims().filter(pk=claim_pk).first()
    return None if row is None else _claim_row(row)


def claim_by_request(request_id: str) -> ClaimRow | None:
    """The claim our ``request_id`` (a UUID string) created; ``None`` for anything else."""
    try:
        row = _claims().filter(request_id=str(request_id)).first()
    except ValueError, ValidationError:
        return None
    return None if row is None else _claim_row(row)


def claim_by_claim_id(order_id: int, claim_id: str) -> ClaimRow | None:
    """The order's claim with Yandex id ``claim_id`` (``None`` when it belongs to no claim of that order)."""
    if not claim_id:
        return None
    row = _claims().filter(shipment__order_id=order_id, claim_id=claim_id).first()
    return None if row is None else _claim_row(row)


def current_claim(order_id: int) -> ClaimRow | None:
    shipment = Shipment.objects.filter(order_id=order_id).values_list("claim_id", flat=True).first()
    return None if shipment is None else claim_row(shipment)


def previous_claim(claim: ClaimRow) -> ClaimRow | None:
    """The shipment's claim created before ``claim`` (the one a new dispatch must find finished)."""
    row = _claims().filter(shipment_id=claim.shipment_id, number__lt=claim.number).order_by("-number").first()
    return None if row is None else _claim_row(row)


def yandex_shipment(order_id: int) -> Shipment | None:
    """The order's Yandex shipment with its current claim and checkout quote (``None`` for BTS / pickup)."""
    return (
        Shipment.objects.select_related("claim", "quote")
        .filter(order_id=order_id, provider=enums.DeliveryProvider.YANDEX)
        .first()
    )


def unsettled_claims() -> list[tuple[int, int, str]]:
    """What the ``bulk_info`` poll reads: every claim Yandex knows (``claim_id`` set) whose cost is not
    final yet — open ones and finished ones still waiting for ``final_price``
    (``courierclaim_unsettled_idx``); ``(claim pk, order id, claim id)`` in id order."""
    rows = (
        CourierClaim.objects.filter(settled_at__isnull=True)
        .exclude(claim_id="")
        .order_by("id")
        .values_list("id", "shipment__order_id", "claim_id")
    )
    return [(int(pk), int(order_id), str(claim_id)) for pk, order_id, claim_id in rows]


def claims_without_final_price(ended_before: datetime) -> list[tuple[int, int]]:
    """Finished, accepted claims Yandex has given no ``final_price`` for since before ``ended_before``:
    ``(claim pk, order id)`` (the poll settles them with ``cost_unknown`` after 24 h)."""
    rows = (
        CourierClaim.objects.filter(
            settled_at__isnull=True,
            ended_at__lt=ended_before,
            accepted_at__isnull=False,
            final_price__isnull=True,
        )
        .order_by("id")
        .values_list("id", "shipment__order_id")
    )
    return [(int(pk), int(order_id)) for pk, order_id in rows]


def unanswered_claims(ended_before: datetime) -> list[tuple[int, int]]:
    """Claims Yandex knows that ended before ``ended_before`` without any acceptance yet are still unsettled
    — closed by a dispatch that could not rule out an acceptance, so the poll kept reading them, and Yandex
    has shown no new status since (a new one settles them through the writer): ``(claim pk, order id)``
    (the poll settles them with ``no_answer`` after 24 h)."""
    rows = (
        CourierClaim.objects.filter(
            settled_at__isnull=True,
            ended_at__lt=ended_before,
            accepted_at__isnull=True,
        )
        .exclude(claim_id="")
        .order_by("id")
        .values_list("id", "shipment__order_id")
    )
    return [(int(pk), int(order_id)) for pk, order_id in rows]


# --- Yandex Delivery: cancellation, returns (plan §3.8) -----------------------------------------------------


def claim_picked_up(shipment_id: int, claim_id: str, status: str = "") -> bool:
    """The claim took the parcel, now or earlier: ``status`` is a picked-up one, or a ``claim_status`` event
    of that claim recorded one (a later failure status — ``failed`` after pickup — hides it from the row)."""
    if status in yandex_statuses.PICKED_UP:
        return True
    if not claim_id:
        return False
    return ShipmentEvent.objects.filter(
        shipment_id=shipment_id,
        kind=ShipmentEvent.Kind.CLAIM_STATUS,
        carrier_status__in=list(yandex_statuses.PICKED_UP),
        payload__claim_id=claim_id,
    ).exists()


def return_confirmed(order_id: int, claim_number: int) -> bool:
    """Staff confirmed that the parcel of the order's claim ``claim_number`` is physically back at the
    warehouse (its ``returned`` shipment event, ``POST orders/{id}/confirm-return/``)."""
    return ShipmentEvent.objects.filter(
        shipment__order_id=order_id, kind=ShipmentEvent.Kind.RETURNED, payload__claim_number=claim_number
    ).exists()


def return_requested(shipment_id: int, claim_id: str) -> bool:
    """Our ``claims/return`` of Yandex claim ``claim_id`` was sent (Yandex answered it, or its outcome is
    unknown): its ``return_requested`` event. Such a return is never sent a second time."""
    if not claim_id:
        return False
    return ShipmentEvent.objects.filter(
        shipment_id=shipment_id, kind=ShipmentEvent.Kind.RETURN_REQUESTED, payload__claim_id=claim_id
    ).exists()


@dataclass(frozen=True, slots=True)
class ReturnClaim:
    """The claim that carried the parcel last: the shipment's highest-numbered claim that took it (its own
    status or one of its ``claim_status`` events is a picked-up one). A return to confirm belongs to it,
    whichever claim a later dispatch made current (a re-dispatch that summoned nobody never carried it)."""

    id: int
    number: int
    claim_id: str
    status: str
    accepted_at: datetime | None
    confirmed: bool  # staff confirmed its parcel back at the warehouse (its ``returned`` event)


def return_claim(shipment_id: int) -> ReturnClaim | None:
    """The shipment's :class:`ReturnClaim` (``None`` when no claim ever took the parcel); one query."""
    took = (
        ShipmentEvent.objects.filter(
            shipment_id=shipment_id,
            kind=ShipmentEvent.Kind.CLAIM_STATUS,
            carrier_status__in=list(yandex_statuses.PICKED_UP),
        )
        .annotate(event_claim=KT("payload__claim_id"))
        .filter(event_claim=OuterRef("claim_id"))
    )
    confirmed = (
        ShipmentEvent.objects.filter(shipment_id=shipment_id, kind=ShipmentEvent.Kind.RETURNED)
        .annotate(event_number=KT("payload__claim_number"))
        .filter(event_number=Cast(OuterRef("number"), output_field=TextField()))
    )
    row = (
        CourierClaim.objects.filter(shipment_id=shipment_id)
        .exclude(claim_id="")
        .annotate(took=Exists(took), confirmed=Exists(confirmed))
        .filter(Q(status__in=list(yandex_statuses.PICKED_UP)) | Q(took=True))
        .order_by("-number")
        .values_list("pk", "number", "claim_id", "status", "accepted_at", "confirmed")
        .first()
    )
    if row is None:
        return None
    pk, number, claim_id, status, accepted_at, is_confirmed = row
    return ReturnClaim(int(pk), int(number), str(claim_id), str(status), accepted_at, bool(is_confirmed))


@dataclass(frozen=True, slots=True)
class YandexCancelView:
    """What the carrier-aware cancel decides from (``commerce.services.cancel_at_carrier`` and the guard of
    ``cancel_order``): the Yandex shipment's stage, its current claim, whether that claim took the parcel
    and — while the parcel is returning / back — the claim that carried it and whether staff confirmed its
    return."""

    shipment_id: int
    state: str  # Shipment.State
    stage: str  # ShipmentStage
    cancel_requested_at: datetime | None
    claim: ClaimRow | None  # the current claim
    claim_open: bool  # a claim of the shipment is open (only the current one can be)
    picked_up: bool  # the current claim took the parcel (now or earlier)
    return_confirmed: bool  # staff confirmed the returning parcel back at the warehouse (its return claim)
    return_claim: ReturnClaim | None = None  # the claim that carried the parcel (read at the return stages)

    @property
    def returning(self) -> bool:
        """The parcel is on its way back or back at the warehouse (plan §3.8 "returning / returned")."""
        return self.stage in yandex_statuses.RETURN_STAGES


def yandex_cancel_view(order_id: int) -> YandexCancelView | None:
    """The order's Yandex shipment as the cancel sees it (``None`` for BTS / pickup); at most four queries."""
    open_claim = CourierClaim.objects.filter(shipment=OuterRef("pk"), ended_at__isnull=True)
    shipment = (
        Shipment.objects.filter(order_id=order_id, provider=enums.DeliveryProvider.YANDEX)
        .annotate(claim_open=Exists(open_claim))
        .first()
    )
    if shipment is None:
        return None
    claim = claim_row(shipment.claim_id) if shipment.claim_id is not None else None
    returned = return_claim(shipment.pk) if shipment.stage in yandex_statuses.RETURN_STAGES else None
    return YandexCancelView(
        shipment_id=shipment.pk,
        state=shipment.state,
        stage=shipment.stage,
        cancel_requested_at=shipment.cancel_requested_at,
        claim=claim,
        claim_open=bool(shipment.claim_open),  # type: ignore[attr-defined]
        picked_up=claim is not None and claim_picked_up(shipment.pk, claim.claim_id, claim.status),
        return_confirmed=returned is not None and returned.confirmed,
        return_claim=returned,
    )


def slow_courier_searches(accepted_before: datetime) -> list[tuple[int, int]]:
    """Current, open claims Yandex accepted before ``accepted_before`` that still have no courier
    (:data:`~glowderma.delivery.yandex.SEARCHING`): ``(order id, claim number)``."""
    rows = (
        CourierClaim.objects.filter(
            ended_at__isnull=True,
            accepted_at__lte=accepted_before,
            status__in=list(yandex_statuses.SEARCHING),
            shipment__provider=enums.DeliveryProvider.YANDEX,
            shipment__claim=F("pk"),
        )
        .order_by("id")
        .values_list("shipment__order_id", "number")
    )
    return [(int(order_id), int(number)) for order_id, number in rows]


def yandex_orders_at(stages: Collection[str]) -> list[int]:
    """The order ids whose Yandex shipment is at one of ``stages`` (``shipment_yandex_stage_idx``)."""
    rows = Shipment.objects.filter(provider=enums.DeliveryProvider.YANDEX, stage__in=list(stages))
    return [int(order_id) for order_id in rows.order_by("order_id").values_list("order_id", flat=True)]


@dataclass(frozen=True, slots=True)
class CancelRequest:
    """A Yandex shipment whose carrier-side cancel was asked for (``cancel_requested_at``) and not cleared."""

    order_id: int
    stage: str
    requested_at: datetime
    claim_number: int | None
    claim_status: str
    claim_ended: bool
    claim_error_code: str = ""  # ``cancel_requested``: withdrawn for the cancel (never accepted)


def cancel_requests(requested_before: datetime) -> list[CancelRequest]:
    """Yandex shipments with a cancel stamp older than ``requested_before`` (the review beat's
    ``cancel_incomplete`` candidates)."""
    rows = (
        Shipment.objects.filter(
            provider=enums.DeliveryProvider.YANDEX, cancel_requested_at__lte=requested_before
        )
        .select_related("claim")
        .order_by("order_id")
    )
    return [
        CancelRequest(
            order_id=row.order_id,
            stage=row.stage,
            requested_at=row.cancel_requested_at,  # type: ignore[arg-type]
            claim_number=row.claim.number if row.claim is not None else None,
            claim_status=row.claim.status if row.claim is not None else "",
            claim_ended=row.claim is not None and row.claim.ended_at is not None,
            claim_error_code=row.claim.error_code if row.claim is not None else "",
        )
        for row in rows
    ]


class IssueFilters(TypedDict, total=False):
    state: str  # DeliveryIssueState
    urgent: bool
    kind: str  # DeliveryIssueKind
    order: int


def issues_queryset(filters: IssueFilters) -> QuerySet[DeliveryIssue]:
    """``GET delivery/issues/``: the queue filtered by ``state`` / ``urgent`` / ``kind`` / ``order`` (with the
    resolver loaded). Open rows first — urgent first, then oldest first (``deliveryissue_queue_idx``); a
    ``state=resolved`` list is newest resolved first."""
    rows = DeliveryIssue.objects.select_related("resolved_by")
    if "state" in filters:
        rows = rows.filter(state=filters["state"])
    if "urgent" in filters:
        rows = rows.filter(urgent=filters["urgent"])
    if "kind" in filters:
        rows = rows.filter(kind=filters["kind"])
    if "order" in filters:
        rows = rows.filter(order_id=filters["order"])
    if filters.get("state") == enums.DeliveryIssueState.RESOLVED:
        return rows.order_by("-resolved_at", "-id")
    open_first = Case(When(state=enums.DeliveryIssueState.OPEN, then=Value(0)), default=Value(1))
    return rows.order_by(open_first, "-urgent", "opened_at", "id")


__all__ = [
    "REGIONS_CACHE_KEY",
    "RegionCatalog",
    "ShipmentInfo",
    "api_state",
    "bts_failed_count",
    "create_request_answered",
    "region_catalog",
    "cached_region_catalog",
    "refresh_region_catalog",
    "region_known",
    "shipment_by_tracking",
    "stale_shipments",
    "shipment_for_order",
    "untracked_parcels",
    "shipment_info",
    "shipment_view",
    "updated_at",
    "ClaimRow",
    "YandexCancelView",
    "CancelRequest",
    "cancel_requests",
    "slow_courier_searches",
    "yandex_orders_at",
    "claim_picked_up",
    "ReturnClaim",
    "return_claim",
    "return_confirmed",
    "return_requested",
    "yandex_cancel_view",
    "claim_by_claim_id",
    "claim_by_request",
    "claim_row",
    "claims_without_final_price",
    "current_claim",
    "previous_claim",
    "unanswered_claims",
    "unsettled_claims",
    "yandex_shipment",
    "IssueFilters",
    "IssueInfo",
    "MethodSettings",
    "issues_queryset",
    "settings_row",
    "has_open_issue",
    "issue",
    "issue_counts",
    "method_settings",
    "open_issues",
    "shipment_provider",
    "warehouse_calendar",
    "warehouse_configured",
]
