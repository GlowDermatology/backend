"""Read side of `commerce` (design 03 §6.1 orders list/search/period, §6.2 status counts, §1.6 available
stock, §6.4 orders per bucket; contract §2.2 ``Order`` detail).

The list reads the display payment and the shipment through the lower modules' selectors
(``payments.selectors.pick_display`` / ``display_status_subquery``, ``delivery.selectors.shipment_view``)
over rows loaded with ``prefetch_related("payments")`` and ``select_related("shipment__create_message")``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypedDict

from django.db.models import Count, IntegerField, OuterRef, Q, QuerySet, Subquery, Sum, TextField, Value
from django.db.models.functions import Cast, Coalesce, TruncDay, TruncHour

from glowderma.catalog.models import ProductStock
from glowderma.commerce import state
from glowderma.commerce.models import Order, OrderEvent, StockReservation
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery.models import Shipment
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.payments import selectors as payment_selectors
from glowderma.payments.models import Payment

SEARCH_MIN_PHONE_DIGITS = 3
STATUS_COUNT_KEYS: tuple[str, ...] = ("all", *enums.OrderStatus.values)


# --- available stock (03 §1.6) ------------------------------------------------------------------------------


def available_stock(product_ids: Iterable[int]) -> dict[int, int]:
    """``{product_id: Billz quantity − active reservations − converted since the last Billz sync}``.

    Billz owns ``ProductStock.quantity``; units of a converted reservation stay unavailable until a Billz
    sync after the conversion reflects the pushed sale (``synced_at`` null → that term is ignored). A product
    without a stock row has 0 available.
    """
    ids = list(product_ids)
    result = dict.fromkeys(ids, 0)
    active = (
        StockReservation.objects.filter(
            product_id=OuterRef("product_id"), state=StockReservation.State.ACTIVE
        )
        .order_by()
        .values("product_id")
        .annotate(total=Sum("quantity"))
        .values("total")[:1]
    )
    converted = (
        StockReservation.objects.filter(
            product_id=OuterRef("product_id"),
            state=StockReservation.State.CONVERTED,
            converted_at__gt=OuterRef("synced_at"),
        )
        .order_by()
        .values("product_id")
        .annotate(total=Sum("quantity"))
        .values("total")[:1]
    )
    rows = (
        ProductStock.objects.filter(product_id__in=ids)
        .annotate(
            held=Coalesce(Subquery(active, output_field=IntegerField()), Value(0)),
            sold=Coalesce(Subquery(converted, output_field=IntegerField()), Value(0)),
        )
        .values_list("product_id", "quantity", "held", "sold")
    )
    for product_id, quantity, held, sold in rows:
        result[int(product_id)] = int(quantity) - int(held) - int(sold)
    return result


def customer_available_stock(product_ids: Iterable[int]) -> dict[int, int]:
    """The one availability read of the customer API (customer contract §1.4, §2.3).

    ``max(0, available_stock)`` per product; **0 for every product** while online selling is off
    (``core.integration_modes.online_sales_enabled()`` false: Billz is fake under the production settings, so
    the stock is unknown). The catalog (P1), the cart and checkout (P2) read availability only through here.
    """
    ids = list(product_ids)
    if not integration_modes.online_sales_enabled():
        return dict.fromkeys(ids, 0)
    return {product_id: max(0, quantity) for product_id, quantity in available_stock(ids).items()}


def customer_inflight_order(customer_id: int) -> int | None:
    """The customer's ``awaiting_payment`` order whose Click payment is in flight (customer contract §2.2.3
    step 1), else ``None``. P0 stub: always ``None``; P2 implements it with
    ``commerce.services.in_flight``."""
    return None


# --- orders list (03 §6.1) ----------------------------------------------------------------------------------


class OrderFilters(TypedDict, total=False):
    status: str
    search: str
    fulfilment: str
    payment: str
    period: str
    date_from: date
    date_to: date
    customer: int
    ordering: str


def order_queryset() -> QuerySet[Order]:
    """Orders with everything the summary renders: customer, display payment, shipment (+ its message)."""
    return Order.objects.select_related("customer", "shipment", "shipment__create_message").prefetch_related(
        "payments"
    )


def search_q(term: str) -> Q:
    """Contract §2.2 search: the order id contains the term without a leading ``№``; or the term has ≥ 3
    digits and the customer phone contains them; or the customer name contains the term (case-insensitive)."""
    number = term.removeprefix("№").strip()
    condition = Q(customer__name__icontains=term)
    if number:
        condition |= Q(id_text__contains=number)
    digits = re.sub(r"\D", "", term)
    if len(digits) >= SEARCH_MIN_PHONE_DIGITS:
        condition |= Q(customer__phone__contains=digits)
    return condition


def filter_orders(
    qs: QuerySet[Order], filters: OrderFilters, *, now: datetime | None = None, with_status: bool = True
) -> QuerySet[Order]:
    """The ``orders/`` predicates (``status`` only when ``with_status``; status-counts drops it)."""
    instant = now or timeutils.now()
    if with_status and "status" in filters:
        qs = qs.filter(status=filters["status"])
    if "fulfilment" in filters:
        qs = qs.filter(fulfilment=filters["fulfilment"])
    if "customer" in filters:
        qs = qs.filter(customer_id=filters["customer"])
    if "payment" in filters:
        qs = qs.annotate(display_payment_status=payment_selectors.display_status_subquery()).filter(
            display_payment_status=filters["payment"]
        )
    bounds = timeutils.period_bounds(filters.get("period", enums.OrderPeriod.ALL), instant)
    qs = qs.filter(**{f"created_at__{key}": value for key, value in bounds.items()})
    days = timeutils.date_range_bounds(filters.get("date_from"), filters.get("date_to"))
    qs = qs.filter(**{f"created_at__{key}": value for key, value in days.items()})
    term = filters.get("search", "").strip()
    if term:
        qs = qs.annotate(id_text=Cast("id", output_field=TextField())).filter(search_q(term))
    return qs


def orders_list(filters: OrderFilters, *, now: datetime | None = None) -> QuerySet[Order]:
    """``GET orders/``: filtered, ordered ``±created_at`` with ties by ``id`` in the same direction."""
    qs = filter_orders(order_queryset(), filters, now=now)
    if filters.get("ordering") == "created_at":
        return qs.order_by("created_at", "id")
    return qs.order_by("-created_at", "-id")


def status_counts(filters: OrderFilters, *, now: datetime | None = None) -> dict[str, int]:
    """``GET orders/status-counts/``: the list's predicates without ``status``; every key present."""
    rows = (
        filter_orders(Order.objects.all(), filters, now=now, with_status=False)
        .order_by()
        .values("status")
        .annotate(n=Count("id"))
        .values_list("status", "n")
    )
    counts = dict.fromkeys(STATUS_COUNT_KEYS, 0)
    for status, n in rows:
        counts[str(status)] = int(n)
    counts["all"] = sum(counts[status] for status in enums.OrderStatus.values)
    return counts


def processing_count() -> int:
    """``nav-counts.orders_processing``: all-time ``processing`` orders."""
    return Order.objects.filter(status=enums.OrderStatus.PROCESSING).count()


# --- summary pieces -----------------------------------------------------------------------------------------


def _shipment_of(order: Order) -> Shipment | None:
    try:
        return order.shipment
    except Shipment.DoesNotExist:
        return None


def payment_summary(order: Order) -> dict[str, Any]:
    """``{status, provider}`` of the display payment (fallback ``pending`` + the checkout provider)."""
    payment = payment_selectors.pick_display(order.payments.all())
    if payment is None:
        return {"status": enums.PaymentStatus.PENDING, "provider": order.payment_provider}
    return {"status": payment.status, "provider": payment.provider}


def payment_detail(order: Order) -> dict[str, Any]:
    """The detail ``payment`` (+ ``amount``, ``transaction_id``, ``confirmed_at``)."""
    payment: Payment | None = payment_selectors.pick_display(order.payments.all())
    if payment is None:
        return {
            "status": enums.PaymentStatus.PENDING,
            "provider": order.payment_provider,
            "amount": order.total,
            "transaction_id": None,
            "confirmed_at": None,
        }
    return {
        "status": payment.status,
        "provider": payment.provider,
        "amount": payment.amount,
        "transaction_id": payment.provider_txn_id or None,
        "confirmed_at": payment.confirmed_at,
    }


def shipment_summary(order: Order) -> dict[str, Any]:
    return delivery_selectors.shipment_view(_shipment_of(order), order.fulfilment)


def tracking_number(order: Order) -> str:
    shipment = _shipment_of(order)
    return shipment.tracking_number if shipment is not None else ""


def cancellation(order: Order) -> dict[str, Any] | None:
    if order.status != enums.OrderStatus.CANCELLED or not order.cancellation_reason:
        return None
    return {"reason": order.cancellation_reason, "note": order.cancellation_note or None}


# --- detail (contract §2.2 ``Order``) -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderDetail:
    order: Order
    items: list[dict[str, Any]]
    payment: dict[str, Any]
    refunds: list[payment_selectors.RefundRow]
    shipment: dict[str, Any]
    cashback_hold: dict[str, Any] | None
    cashback: list[dict[str, Any]]
    history: list[OrderEvent]
    available_actions: dict[str, Any]


def _cashback_hold(order_id: int) -> dict[str, Any] | None:
    """``Order.cashback_hold``: the order's ``active`` hold only, else ``null`` (contract §7 item 18)."""
    hold = loyalty_selectors.hold_for_order(order_id)
    if hold is None or hold.state != enums.CashbackHoldState.ACTIVE:
        return None
    return {"amount": hold.amount, "state": hold.state, "expires_at": hold.expires_at}


def available_actions(order: Order, shipment: Shipment | None) -> dict[str, Any]:
    """03 §4.2: ``transitions`` (state.py manual rows), ``cancel``, ``retry_shipment``."""
    pending = shipment.is_pending if shipment is not None else False
    shipment_state = delivery_selectors.api_state(shipment, order.fulfilment)
    return {
        "transitions": state.manual_transitions(order.status, order.fulfilment),
        "cancel": state.can_cancel(order.status),
        "retry_shipment": state.can_retry_shipment(order.fulfilment, order.status, shipment_state, pending),
    }


def order_detail(order_id: int) -> OrderDetail | None:
    """Everything ``GET orders/{id}/`` renders, in a handful of queries; ``None`` → 404."""
    order = order_queryset().filter(pk=order_id).first()
    if order is None:
        return None
    shipment = _shipment_of(order)
    items = [
        {
            "product_id": item.product_id,
            "name": item.name,
            "billz_id": item.billz_id,
            "image_url": item.image_url or None,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "promo": item.promo,
        }
        for item in order.items.order_by("position")
    ]
    cashback = [
        {
            "ledger_entry_id": row.id,
            "kind": row.kind,
            "amount": row.amount,
            "at": row.at,
            "base": row.base,
            "percent": row.percent,
            "reverses_id": row.reverses_id,
        }
        for row in loyalty_selectors.ledger_for_order(order.pk)
    ]
    history = list(OrderEvent.objects.select_related("actor").filter(order=order).order_by("-at", "-id"))
    return OrderDetail(
        order=order,
        items=items,
        payment=payment_detail(order),
        refunds=payment_selectors.refunds_for_order(order.pk),
        shipment=delivery_selectors.shipment_view(shipment, order.fulfilment),
        cashback_hold=_cashback_hold(order.pk),
        cashback=cashback,
        history=history,
        available_actions=available_actions(order, shipment),
    )


# --- dashboard (03 §6.4) ------------------------------------------------------------------------------------


def orders_in_buckets(start: datetime, end: datetime, unit: str) -> dict[datetime, int]:
    """``{bucket start (Tashkent, aware): orders created}`` in ``[start, end)``; ``unit`` ``hour``/``day``."""
    trunc = TruncHour if unit == enums.DashboardBucket.HOUR else TruncDay
    rows = (
        Order.objects.filter(created_at__gte=start, created_at__lt=end)
        .annotate(bucket=trunc("created_at", tzinfo=TASHKENT))
        .order_by()
        .values("bucket")
        .annotate(n=Count("id"))
        .values_list("bucket", "n")
    )
    return {bucket.astimezone(TASHKENT): int(n) for bucket, n in rows}


def count_created(start: datetime, end: datetime) -> int:
    """Orders created in ``[start, end)`` (``previous_period_orders``)."""
    return Order.objects.filter(created_at__gte=start, created_at__lt=end).count()


__all__ = [
    "STATUS_COUNT_KEYS",
    "OrderDetail",
    "OrderFilters",
    "available_actions",
    "available_stock",
    "customer_available_stock",
    "customer_inflight_order",
    "cancellation",
    "count_created",
    "filter_orders",
    "order_detail",
    "order_queryset",
    "orders_in_buckets",
    "orders_list",
    "payment_detail",
    "payment_summary",
    "processing_count",
    "search_q",
    "shipment_summary",
    "status_counts",
    "tracking_number",
]
