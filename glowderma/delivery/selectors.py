"""Read side of `delivery` (design 02 §3.1 API rendering, derived ``pending``; 03 §6.4 ``bts_failed``).

The contract's ``shipment`` union (``OrderSummary.shipment``):

| state | keys |
|---|---|
| ``not_required`` | — (pickup: no row) |
| ``not_created`` | ``pending`` |
| ``failed`` | ``last_attempt_at``, ``attempts``, ``pending`` |
| ``created`` | ``tracking_number``, ``tariff``, ``updated_at``, ``via`` (also a ``cancelled`` parcel, D-1) |

``pending`` is true while the newest ``bts.create_shipment`` message is ``pending``/``failed``/``in_flight``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from django.core.cache import cache
from django.db.models import Q
from django.db.models.functions import Coalesce

from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.consistency import Finding, finding
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.integrations.bts.ports import City, Region
from glowderma.integrations.bts.registry import get_bts

# BTS's region/city reference list (customer contract §2.6): cached 24 h under this key.
REGIONS_CACHE_KEY = "gd:bts:regions"
REGIONS_CACHE_SECONDS = 24 * 3600


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


def shipment_info(order_id: int) -> ShipmentInfo | None:
    """The order's shipment (``None`` for pickup orders and orders without a row)."""
    row = Shipment.objects.with_pending().filter(order_id=order_id).first()
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
    )


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
    """Dashboard ``attention.bts_failed``: processing orders whose shipment is ``failed`` (02 §3.1)."""
    return Shipment.objects.filter(
        state=Shipment.State.FAILED, order__status=enums.OrderStatus.PROCESSING
    ).count()


@dataclass(frozen=True, slots=True)
class RegionCatalog:
    regions: list[Region]
    updated_at: datetime | None


def region_catalog() -> RegionCatalog:
    """``delivery/regions/``: BTS ``regions()`` cached 24 h (``gd:bts:regions``); empty with ``updated_at``
    null (nothing cached, no BTS call) while delivery is not offered (customer contract §2.6). A transport
    failure of the first fetch raises ``BtsUnavailable`` (the caller answers 503)."""
    if not integration_modes.delivery_available():
        return RegionCatalog(regions=[], updated_at=None)
    cached = cache.get(REGIONS_CACHE_KEY)
    if isinstance(cached, dict):
        regions = [
            Region(code=r["code"], name=r["name"], cities=tuple(City(**c) for c in r["cities"]))
            for r in cached["regions"]
        ]
        return RegionCatalog(regions=regions, updated_at=datetime.fromisoformat(cached["updated_at"]))
    regions = get_bts().regions()
    now = timeutils.now()
    cache.set(
        REGIONS_CACHE_KEY,
        {"regions": [asdict(region) for region in regions], "updated_at": now.isoformat()},
        REGIONS_CACHE_SECONDS,
    )
    return RegionCatalog(regions=list(regions), updated_at=now)


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
        Shipment.objects.filter(
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
    """The shipment whose stored tracking number is ``tracking_number`` (BTS webhook lookup)."""
    if not tracking_number:
        return None
    order_id = (
        Shipment.objects.filter(tracking_number=tracking_number).values_list("order_id", flat=True).first()
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
        Shipment.objects.filter(
            state=Shipment.State.CREATED,
            order__status__in=[enums.OrderStatus.PROCESSING, enums.OrderStatus.HANDED_TO_COURIER],
        )
        .annotate(reference=Coalesce("last_status_at", "bts_created_at"))
        .filter(reference__lt=status_before)
    )
    return finding(queryset, limit=limit, id_field="order_id")


__all__ = [
    "REGIONS_CACHE_KEY",
    "RegionCatalog",
    "ShipmentInfo",
    "api_state",
    "bts_failed_count",
    "create_request_answered",
    "region_catalog",
    "region_known",
    "shipment_by_tracking",
    "stale_shipments",
    "shipment_for_order",
    "untracked_parcels",
    "shipment_info",
    "shipment_view",
    "updated_at",
]
