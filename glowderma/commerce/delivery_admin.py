"""Admin-side reads of an order's delivery (Yandex Delivery plan §3.10): the ``delivery`` object of the admin
order summary and detail, the ``provider`` / ``stage`` / ``attention`` filters of ``orders/`` and
``orders/status-counts/``, and the dashboard's ``fulfilment`` counters.

``delivery`` is ``null`` for a pickup order. A BTS order carries ``provider: "bts"`` and its open issues;
every Yandex-only key is ``null`` for it. The existing ``shipment`` union of the contract is untouched
(superset). The courier's phone is never read here: customers and staff see the name and the vehicle only.

``commerce.selectors`` calls into this module (it never imports ``commerce.selectors``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from django.db.models import Count, Exists, OuterRef, Prefetch, Q, QuerySet

from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes, timeutils
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery.models import DeliveryIssue, Shipment
from glowderma.integrations import selectors as integration_selectors

_STAGE = enums.ShipmentStage
_STATUS = enums.OrderStatus
_YANDEX = enums.DeliveryProvider.YANDEX
_BTS = enums.DeliveryProvider.BTS
_OPEN = enums.DeliveryIssueState.OPEN

# ``attention`` filter values: an open issue of any kind, or an open urgent one.
ATTENTION_OPEN = "open"
ATTENTION_URGENT = "urgent"
ATTENTION_VALUES: tuple[str, ...] = (ATTENTION_OPEN, ATTENTION_URGENT)

# The prefetched open issues of a listed order (:func:`with_delivery`).
OPEN_ISSUES_ATTR = "open_delivery_issues"

# Every key of the ``delivery`` object other than ``provider`` and ``open_issues`` is Yandex-only.
YANDEX_KEYS: tuple[str, ...] = (
    "method",
    "stage",
    "promised_from",
    "promised_to",
    "quote_fee",
    "carrier_price",
    "offer_price",
    "approved_price",
    "carrier_cost_total",
    "cost_difference",
    "parcel_weight_g",
    "prepared_at",
    "courier_name",
    "courier_vehicle",
    "tracking_url",
    "claim_status",
    "claim_number",
    "failure_code",
)

# Dashboard ``fulfilment`` stages (plan §3.10).
PREPARE_STAGES = (_STAGE.AWAITING_DISPATCH, _STAGE.FAILED, _STAGE.RETURNED)
COURIER_STAGES = (_STAGE.COURIER_REQUESTED, _STAGE.COURIER_ASSIGNED, _STAGE.COURIER_ARRIVED)
RETURN_STAGES = (_STAGE.RETURNING, _STAGE.RETURNED)
OPEN_ORDER_STATUSES = (_STATUS.PROCESSING, _STATUS.HANDED_TO_COURIER)


# --- filters ------------------------------------------------------------------------------------------------


def _open_issue(urgent_only: bool) -> Exists:
    rows = DeliveryIssue.objects.filter(order=OuterRef("pk"), state=_OPEN)
    if urgent_only:
        rows = rows.filter(urgent=True)
    return Exists(rows)


def filter_delivery(qs: QuerySet[Order], filters: Mapping[str, object]) -> QuerySet[Order]:
    """``provider`` (the shipment's carrier: pickup orders never match), ``stage`` (a Yandex shipment's
    ``ShipmentStage``) and ``attention`` (``open``: an open delivery issue; ``urgent``: an open urgent
    one)."""
    if "provider" in filters:
        qs = qs.filter(shipment__provider=filters["provider"])
    if "stage" in filters:
        qs = qs.filter(shipment__provider=_YANDEX, shipment__stage=filters["stage"])
    if "attention" in filters:
        qs = qs.filter(_open_issue(filters["attention"] == ATTENTION_URGENT))
    return qs


# --- the delivery object ------------------------------------------------------------------------------------


def open_issue_rows() -> QuerySet[DeliveryIssue]:
    """Open issues as the order card lists them: urgent first, then oldest first."""
    return DeliveryIssue.objects.filter(state=_OPEN).order_by("-urgent", "opened_at", "id")


def with_delivery(qs: QuerySet[Order]) -> QuerySet[Order]:
    """The list rows plus what their ``delivery`` object reads: the current claim and the quote (joined) and
    the open issues (one prefetch query per page), so the list's query count does not grow with the rows."""
    return qs.select_related("shipment__claim", "shipment__quote").prefetch_related(
        Prefetch("delivery_issues", queryset=open_issue_rows(), to_attr=OPEN_ISSUES_ATTR)
    )


def _shipment_of(order: Order) -> Shipment | None:
    try:
        return order.shipment
    except Shipment.DoesNotExist:
        return None


def _issue_view(row: DeliveryIssue) -> dict[str, Any]:
    return {
        "id": row.pk,
        "kind": row.kind,
        "code": row.code,
        "urgent": row.urgent,
        "opened_at": row.opened_at,
    }


def delivery_view(
    order: Order, shipment: Shipment | None, issues: list[DeliveryIssue] | None = None
) -> dict[str, Any] | None:
    """The ``delivery`` object of ``order`` (``None`` for pickup). ``shipment`` should come with its
    ``claim`` and ``quote`` loaded; ``issues`` are the order's open issues (read here when not given)."""
    if order.fulfilment == enums.FulfilmentMethod.PICKUP:
        return None
    if issues is None:
        issues = getattr(order, OPEN_ISSUES_ATTR, None)
    if issues is None:
        issues = list(open_issue_rows().filter(order_id=order.pk))
    provider = shipment.provider if shipment is not None else _BTS
    view: dict[str, Any] = {"provider": provider, **dict.fromkeys(YANDEX_KEYS)}
    view["open_issues"] = [_issue_view(row) for row in issues]
    if shipment is None or provider != _YANDEX:
        return view
    claim = shipment.claim
    quote = shipment.quote
    view.update(
        {
            "method": shipment.method or None,
            "stage": shipment.stage or None,
            "promised_from": shipment.promised_from,
            "promised_to": shipment.promised_to,
            "quote_fee": quote.customer_fee if quote is not None else None,
            "carrier_price": quote.carrier_price if quote is not None else None,
            "carrier_cost_total": shipment.carrier_cost_total,
            "cost_difference": shipment.cost_difference,
            "parcel_weight_g": shipment.parcel_weight_g,
            "prepared_at": shipment.prepared_at,
        }
    )
    if claim is not None:
        view.update(
            {
                "offer_price": claim.offer_price,
                "approved_price": claim.approved_price,
                "courier_name": claim.courier_name or None,
                "courier_vehicle": claim.courier_vehicle or None,
                "tracking_url": claim.tracking_url or None,
                "claim_status": claim.status or None,
                "claim_number": claim.number,
                "failure_code": claim.error_code or None,
            }
        )
    return view


def summary_delivery(order: Order) -> dict[str, Any] | None:
    """The list row's ``delivery`` (rows from :func:`with_delivery`; a lone order reads what it misses)."""
    return delivery_view(order, _shipment_of(order))


def detail_delivery(order: Order, shipment: Shipment | None) -> dict[str, Any] | None:
    """The detail's ``delivery``: pickup reads nothing; a delivery order reads its open issues (one query)
    and, for Yandex, its shipment with the claim and the quote (one more)."""
    if order.fulfilment == enums.FulfilmentMethod.PICKUP:
        return None
    if shipment is not None and shipment.provider == _YANDEX and not yandex_rows_loaded(shipment):
        shipment = load_yandex_rows(shipment)
    return delivery_view(order, shipment, list(open_issue_rows().filter(order_id=order.pk)))


def yandex_rows_loaded(shipment: Shipment) -> bool:
    """The shipment came with its current claim and its quote (a ``None`` FK counts as loaded)."""
    return bool(Shipment.claim.is_cached(shipment) and Shipment.quote.is_cached(shipment))


def load_yandex_rows(shipment: Shipment) -> Shipment:
    """The Yandex shipment again with its current claim, quote and create message joined (one query)."""
    return Shipment.objects.select_related("claim", "quote", "create_message").get(pk=shipment.pk)


# --- dashboard ``fulfilment`` (plan §3.10) ------------------------------------------------------------------


def fulfilment_counts(now: datetime | None = None) -> dict[str, int]:
    """The dashboard's order-state counters (two queries: one over the orders, one over the queue).

    * ``paid_to_prepare`` — ``processing`` orders still to be prepared: Yandex stage ``awaiting_dispatch``,
      or ``failed`` / ``returned`` waiting for a new dispatch; BTS orders not handed over; pickup orders;
    * ``to_hand_to_courier`` — ``processing`` Yandex orders whose courier is requested, assigned or arrived;
    * ``in_delivery`` — ``handed_to_courier`` orders that are not coming back;
    * ``returning`` — Yandex stage ``returning`` / ``returned`` while the order is still open;
    * ``completed_today`` — delivered today (Asia/Tashkent), pickup hand-overs included;
    * ``needs_action`` / ``urgent`` — open / open urgent delivery issues.
    """
    instant = now or timeutils.now()
    start, end = timeutils.day_bounds(timeutils.local_date(instant))
    yandex = Q(shipment__provider=_YANDEX)
    processing = Q(status=_STATUS.PROCESSING)
    bts = Q(fulfilment=enums.FulfilmentMethod.DELIVERY) & (
        Q(shipment__provider=_BTS) | Q(shipment__isnull=True)
    )
    to_prepare = processing & (
        Q(fulfilment=enums.FulfilmentMethod.PICKUP) | bts | (yandex & Q(shipment__stage__in=PREPARE_STAGES))
    )
    returning_stage = yandex & Q(shipment__stage__in=RETURN_STAGES)
    row = Order.objects.filter(
        Q(status__in=OPEN_ORDER_STATUSES) | Q(status=_STATUS.DELIVERED, delivered_at__gte=start)
    ).aggregate(
        paid_to_prepare=Count("id", filter=to_prepare),
        to_hand_to_courier=Count("id", filter=processing & yandex & Q(shipment__stage__in=COURIER_STAGES)),
        handed=Count("id", filter=Q(status=_STATUS.HANDED_TO_COURIER)),
        handed_returning=Count("id", filter=Q(status=_STATUS.HANDED_TO_COURIER) & returning_stage),
        returning=Count("id", filter=Q(status__in=OPEN_ORDER_STATUSES) & returning_stage),
        completed_today=Count(
            "id", filter=Q(status=_STATUS.DELIVERED, delivered_at__gte=start, delivered_at__lt=end)
        ),
    )
    issues = delivery_selectors.issue_counts()
    return {
        "paid_to_prepare": int(row["paid_to_prepare"] or 0),
        "to_hand_to_courier": int(row["to_hand_to_courier"] or 0),
        "in_delivery": int(row["handed"] or 0) - int(row["handed_returning"] or 0),
        "returning": int(row["returning"] or 0),
        "completed_today": int(row["completed_today"] or 0),
        "needs_action": issues["open"],
        "urgent": issues["urgent"],
    }


def yandex_failing() -> bool:
    """``attention.yandex_failing``: Yandex is live and its last poll or price check failed
    (``IntegrationState(yandex).consecutive_failures > 0``, like ``bts_polling_failing``); always ``False``
    in fake mode (no query)."""
    if integration_modes.provider_mode("yandex") != enums.IntegrationMode.LIVE:
        return False
    state = integration_selectors.provider_state("yandex")
    return state is not None and state.consecutive_failures > 0


__all__ = [
    "ATTENTION_OPEN",
    "ATTENTION_URGENT",
    "ATTENTION_VALUES",
    "OPEN_ISSUES_ATTR",
    "YANDEX_KEYS",
    "delivery_view",
    "detail_delivery",
    "filter_delivery",
    "fulfilment_counts",
    "load_yandex_rows",
    "yandex_rows_loaded",
    "open_issue_rows",
    "summary_delivery",
    "with_delivery",
    "yandex_failing",
]
