"""Read side of `commerce` (design 03 §6.1 orders list/search/period, §6.2 status counts, §1.6 available
stock, §6.4 orders per bucket; contract §2.2 ``Order`` detail).

The list reads the display payment and the shipment through the lower modules' selectors
(``payments.selectors.pick_display`` / ``display_status_subquery``, ``delivery.selectors.shipment_view``)
over rows loaded with ``prefetch_related("payments")`` and ``select_related("shipment__create_message")``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol, TypedDict

from django.conf import settings
from django.db.models import Count, IntegerField, OuterRef, Q, QuerySet, Subquery, Sum, TextField, Value
from django.db.models.functions import Cast, Coalesce, TruncDay, TruncHour

from glowderma.accounts.models import Customer
from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import state
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, StockReservation
from glowderma.core import enums, i18n, integration_modes, timeutils
from glowderma.core.media import absolute_media_url
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery.models import Shipment
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.payments import selectors as payment_selectors
from glowderma.payments.models import LATE_CAPTURE, Payment, Refund
from glowderma.site_config.models import SiteSettings

SEARCH_MIN_PHONE_DIGITS = 3
STATUS_COUNT_KEYS: tuple[str, ...] = ("all", *enums.OrderStatus.values)


# --- available stock (03 §1.6) ------------------------------------------------------------------------------


def available_stock(product_ids: Iterable[int]) -> dict[int, int]:
    """``{product_id: Billz quantity − active reservations − converted units Billz has not seen sold}``.

    Billz owns ``ProductStock.quantity`` (read at ``synced_at``). A converted reservation (a paid order) keeps
    counting until its sale is in Billz **and** the stock was read after that (review A M3): while the
    purchase is unpushed (``billz_pushed_at`` null — a push still running, deferred for a closed shift or
    dead-lettered for staff) Billz still counts the unit as in stock, so every sync would otherwise offer it
    again. It also counts while converted after the last read (``converted_at > synced_at``). It stops
    counting once a read after the push reflects the sale, and for a cancelled order whose sale never reached
    Billz (the unit never left Billz's stock). ``synced_at`` null (never read from Billz) → converted units
    are ignored. A product without a stock row has 0 available.
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
    unseen_by_billz = (
        Q(converted_at__gt=OuterRef("synced_at"))
        | Q(order__purchase__billz_pushed_at__gte=OuterRef("synced_at"))
        | (
            Q(order__purchase__billz_pushed_at__isnull=True)
            & ~Q(order__status=enums.OrderStatus.CANCELLED)
            & Q(converted_at__lte=OuterRef("synced_at"))  # null synced_at → never true: ignored
        )
    )
    converted = (
        StockReservation.objects.filter(
            unseen_by_billz, product_id=OuterRef("product_id"), state=StockReservation.State.CONVERTED
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


def image_url(path: str) -> str | None:
    """``OrderItem.image_url`` holds the media storage path; the absolute URL is rendered here, from
    ``PUBLIC_MEDIA_BASE_URL`` (``""`` → ``None``)."""
    return absolute_media_url(path) if path else None


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
            "image_url": image_url(item.image_url),
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
    "OpenMoney",
    "actions",
    "attached_at",
    "cart_view",
    "cashback_max",
    "customer_inflight_order",
    "customer_order",
    "customer_order_detail",
    "customer_orders",
    "customer_timeline",
    "honoured_paid",
    "in_flight",
    "inflight_payment",
    "is_open",
    "is_payable",
    "order_summary",
    "paid_by_other",
    "product_name",
    "progress",
    "provider_open_money",
    "OrderDetail",
    "OrderFilters",
    "available_actions",
    "available_stock",
    "image_url",
    "customer_available_stock",
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


# === M2 customer API (customer contract §2.5–§2.8, §4; plan §3 P2)
# ===========================================

_S = enums.OrderStatus
_E = enums.OrderEventType
_CLICK = enums.PaymentProvider.CLICK
_PAYME = enums.PaymentProvider.PAYME
PAYME_STATE_CREATED = 1
PROVIDER_BRANDS: dict[str, str] = {_CLICK: "Click", _PAYME: "Payme"}
OPEN_ORDER_SCOPES: dict[str, tuple[str, ...]] = {
    enums.OrderScope.ACTIVE: (_S.AWAITING_PAYMENT, _S.PROCESSING, _S.HANDED_TO_COURIER),
    enums.OrderScope.COMPLETED: (_S.DELIVERED,),
    enums.OrderScope.CANCELLED: (_S.CANCELLED,),
}
CUSTOMER_REFUND_STATE: dict[str, str] = {
    enums.RefundState.REQUESTED: enums.CustomerRefundState.IN_PROGRESS,
    enums.RefundState.SUBMITTING: enums.CustomerRefundState.IN_PROGRESS,
    enums.RefundState.MANUAL_REQUIRED: enums.CustomerRefundState.IN_PROGRESS,
    enums.RefundState.UNKNOWN: enums.CustomerRefundState.IN_PROGRESS,
    enums.RefundState.FAILED: enums.CustomerRefundState.IN_PROGRESS,
    enums.RefundState.SUCCEEDED: enums.CustomerRefundState.COMPLETED,
}


class PaymentLike(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def provider_state(self) -> Any: ...


class OrderLike(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def reservation_expires_at(self) -> datetime | None: ...


# --- payment definitions (customer contract §2.7.2: one definition for the app, the jobs and every callback)
# --


def attached_at(payment: PaymentLike) -> datetime | None:
    """When the payment got its provider transaction: Click ``provider_state.prepare_at`` (a Prepare answered
    ``0``), Payme ``provider_state.create_time`` (ms; a ``CreateTransaction`` answered state 1)."""
    state = payment.provider_state if isinstance(payment.provider_state, dict) else {}
    if payment.provider == _CLICK:
        raw = state.get("prepare_at")
        if isinstance(raw, str) and raw:
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None
        return None
    created = state.get("create_time")
    if isinstance(created, int) and not isinstance(created, bool):
        return datetime.fromtimestamp(created / 1000, tz=TASHKENT)
    return None


def is_open(payment: PaymentLike) -> bool:
    """A ``pending`` payment with an attached, uncompleted transaction (Click: no Complete yet; Payme: state
    1)."""
    if payment.status != enums.PaymentStatus.PENDING or attached_at(payment) is None:
        return False
    state = payment.provider_state if isinstance(payment.provider_state, dict) else {}
    if payment.provider == _CLICK:
        return "complete_at" not in state
    return state.get("state") == PAYME_STATE_CREATED


def in_flight(payment: PaymentLike, now: datetime) -> bool:
    """An open transaction the customer may be confirming right now: Click for ``CLICK_INFLIGHT_SECONDS``
    after its Prepare, Payme while in state 1."""
    if not is_open(payment):
        return False
    if payment.provider == _CLICK:
        prepared = attached_at(payment)
        return prepared is not None and now < prepared + timedelta(seconds=settings.CLICK_INFLIGHT_SECONDS)
    return True


def is_payable(order: OrderLike, payment: PaymentLike | None, now: datetime, *, attaching: bool) -> bool:
    """``awaiting_payment`` ∧ (``now < reservation_expires_at`` ∨ (completing a transaction attached before
    it, within ``PAYMENT_IN_FLIGHT_GRACE_SECONDS``)). Starting a transaction (``attaching``) never gets the
    grace."""
    expires = order.reservation_expires_at
    if order.status != _S.AWAITING_PAYMENT or expires is None:
        return False
    if now < expires:
        return True
    if attaching or payment is None:
        return False
    attached = attached_at(payment)
    grace = timedelta(seconds=settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS)
    return attached is not None and attached < expires and now < expires + grace


def orders_in_payment_grace(order_ids: Collection[int], now: datetime) -> set[int]:
    """The ``awaiting_payment`` orders among ``order_ids`` past ``reservation_expires_at`` whose pending
    payment has an open transaction attached before it, within ``PAYMENT_IN_FLIGHT_GRACE_SECONDS`` — the
    orders the expiry job skips (``services._in_grace``). Registered as ``loyalty.hold_grace``'s oracle."""
    grace = timedelta(seconds=settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS)
    kept: set[int] = set()
    overdue = Order.objects.filter(
        id__in=list(order_ids), status=_S.AWAITING_PAYMENT, reservation_expires_at__lte=now
    ).values_list("id", "reservation_expires_at")
    for order_id, expires in overdue:
        payment = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
        if payment is None or not is_open(payment):
            continue
        attached = attached_at(payment)
        if expires is not None and attached is not None and attached < expires and now < expires + grace:
            kept.add(order_id)
    return kept


def _pending_payments(order_id: int) -> list[payment_selectors.PaymentRow]:
    return [
        row
        for row in payment_selectors.payments_of_order(order_id)
        if row.status == enums.PaymentStatus.PENDING and not row.late_capture
    ]


def inflight_payment(order_id: int, now: datetime | None = None) -> payment_selectors.PaymentRow | None:
    """The order's pending payment whose transaction is in flight, else ``None``."""
    instant = now or timeutils.now()
    return next((row for row in _pending_payments(order_id) if in_flight(row, instant)), None)


def customer_inflight_order(customer_id: int) -> int | None:
    """The customer's ``awaiting_payment`` order whose **Click** payment is in flight (customer contract
    §2.2.3 step 1: ``DELETE me/`` answers 409 ``payment_in_progress``), else ``None``."""
    now = timeutils.now()
    for order_id in Order.objects.filter(customer_id=customer_id, status=_S.AWAITING_PAYMENT).values_list(
        "id", flat=True
    ):
        payment = inflight_payment(int(order_id), now)
        if payment is not None and payment.provider == _CLICK:
            return int(order_id)
    return None


def paid_by_other(order_id: int, payment_id: int) -> bool:
    """Another payment of the order (not a late capture) is ``paid`` / ``refunded`` — a double payment."""
    return (
        Payment.objects.filter(
            order_id=order_id, status__in=[enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED]
        )
        .exclude(pk=payment_id)
        .exclude(error_code=LATE_CAPTURE)
        .exists()
    )


def honoured_paid(order_id: int) -> bool:
    """The order has a non-late ``paid`` payment (``already_paid`` of ``orders/{id}/pay/``)."""
    return (
        Payment.objects.filter(order_id=order_id, status=enums.PaymentStatus.PAID)
        .exclude(error_code=LATE_CAPTURE)
        .exists()
    )


def cashback_max(items_subtotal: int, delivery_fee: int, available: int | None) -> int:
    """The one cashback cap of the cart, ``delivery/calculate/`` and checkout (customer contract §2.7):
    ``max(0, min(available, items_subtotal, items_subtotal + delivery_fee − CHECKOUT_MIN_PAYABLE))``;
    ``available=None`` is ∞ (checkout row 8)."""
    cap = min(items_subtotal, items_subtotal + delivery_fee - int(settings.CHECKOUT_MIN_PAYABLE))
    if available is not None:
        cap = min(cap, available)
    return max(0, cap)


# --- texts
# ----------------------------------------------------------------------------------------------------


def product_name(product: Product, lang: str) -> str:
    """The resolved-language name → uz → ``billz_name`` (customer contract §1.4)."""
    return str(getattr(product, f"name_{lang}", "") or product.name_uz or product.billz_name or "")


def product_image(product: Product) -> str | None:
    images = sorted(product.images.all(), key=lambda image: (image.position, image.pk))
    return absolute_media_url(images[0].file.name or "") if images and images[0].file else None


def provider_brand(provider: str) -> str:
    return PROVIDER_BRANDS.get(provider, provider)


def status_label(status: str, fulfilment: str, lang: str) -> str:
    if status == _S.DELIVERED and fulfilment == enums.FulfilmentMethod.PICKUP:
        return i18n.t("label.order_status_pickup.delivered", lang)
    return i18n.t(f"label.order_status.{status}", lang)


def pickup_address(lang: str) -> str:
    site = SiteSettings.load()
    return str(getattr(site, f"pickup_address_{lang}", "") or site.pickup_address_uz or "")


# --- cart (customer contract §2.5)
# ------------------------------------------------------------------------------


def max_quantity(available: int) -> int:
    return max(0, min(available, int(settings.CART_MAX_QTY_PER_LINE)))


def line_status(visible: bool, available: int, quantity: int) -> str:
    """``unavailable`` (not visible) · ``out_of_stock`` (available ≤ 0) · ``insufficient`` (quantity >
    max_quantity > 0) · ``ok``."""
    if not visible:
        return enums.CartLineStatus.UNAVAILABLE
    if available <= 0:
        return enums.CartLineStatus.OUT_OF_STOCK
    limit = max_quantity(available)
    if quantity > limit > 0:
        return enums.CartLineStatus.INSUFFICIENT
    return enums.CartLineStatus.OK


def visible_ids(product_ids: Iterable[int]) -> set[int]:
    return set(
        catalog_selectors.customer_visible_products()
        .filter(pk__in=list(product_ids))
        .values_list("pk", flat=True)
    )


def cart_view(customer_id: int, lang: str, *, last_change: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``Cart`` of customer contract §2.5 (a GET never changes stored quantities)."""
    cart = Cart.objects.filter(customer_id=customer_id).first()
    items: list[CartItem] = []
    if cart is not None:
        items = list(
            CartItem.objects.filter(cart=cart)
            .select_related("product")
            .prefetch_related("product__images")
            .order_by("added_at", "id")
        )
    product_ids = [item.product_id for item in items]
    available = customer_available_stock(product_ids)
    visible = visible_ids(product_ids)
    lines = []
    subtotal = promo_subtotal = quantity_total = 0
    has_problems = False
    for item in items:
        product = item.product
        stock = available.get(product.pk, 0)
        limit = max_quantity(stock)
        status = line_status(product.pk in visible, stock, item.quantity)
        line_total = product.price * item.quantity
        if status == enums.CartLineStatus.OK:
            subtotal += line_total
            if product.promo:
                promo_subtotal += line_total
        else:
            has_problems = True
        quantity_total += item.quantity
        changed = item.price_at_add is not None and item.price_at_add != product.price
        lines.append(
            {
                "product_id": product.pk,
                "name": product_name(product, lang),
                "volume": product.volume,
                "main_image_url": product_image(product),
                "unit_price": product.price,
                "promo": product.promo,
                "quantity": item.quantity,
                "line_total": line_total,
                "available": stock,
                "max_quantity": limit,
                "max_reached": item.quantity >= limit > 0,
                "status": status,
                "price_changed": changed,
                "previous_price": item.price_at_add if changed else None,
            }
        )
    balance = loyalty_selectors.account_summary(customer_id).available
    return {
        "items": lines,
        "item_count": len(lines),
        "quantity_total": quantity_total,
        "items_subtotal": subtotal,
        "promo_subtotal": promo_subtotal,
        "has_problems": has_problems,
        "cashback": {"available": balance, "max_usable_pickup": cashback_max(subtotal, 0, balance)},
        "updated_at": cart.updated_at if cart is not None else None,
        "last_change": last_change,
    }


# --- customer orders (customer contract §2.8)
# ------------------------------------------------------------------


def history_from(customer_id: int) -> datetime | None:
    return Customer.objects.filter(pk=customer_id).values_list("history_from", flat=True).first()


def customer_orders(customer_id: int, scope: str = enums.OrderScope.ALL) -> QuerySet[Order]:
    """``me/orders/``: the customer's orders created at or after the history horizon, newest first."""
    qs = Order.objects.filter(customer_id=customer_id)
    horizon = history_from(customer_id)
    if horizon is not None:
        qs = qs.filter(created_at__gte=horizon)
    statuses = OPEN_ORDER_SCOPES.get(scope)
    if statuses is not None:
        qs = qs.filter(status__in=list(statuses))
    return (
        qs.select_related("shipment")
        .prefetch_related("payments", "items", "items__product", "items__product__images")
        .order_by("-created_at", "-id")
    )


def customer_order(order_id: int, customer_id: int) -> Order | None:
    """The customer's own order within the history horizon (else ``None`` → 404)."""
    return customer_orders(customer_id).filter(pk=order_id).first()


def _payments(order: Order) -> list[Payment]:
    return list(order.payments.all())


def payment_in_progress(order: Order, now: datetime) -> bool:
    return order.status == _S.AWAITING_PAYMENT and any(in_flight(p, now) for p in _payments(order))


def actions(order: Order, now: datetime) -> dict[str, bool]:
    """``actions`` (customer contract §2.8)."""
    awaiting = order.status == _S.AWAITING_PAYMENT
    payme_open = any(p.provider == _PAYME and is_open(p) for p in _payments(order))
    click_inflight = any(p.provider == _CLICK and in_flight(p, now) for p in _payments(order))
    expired = order.reservation_expires_at is not None and now >= order.reservation_expires_at
    return {
        "pay": awaiting and is_payable(order, None, now, attaching=True) and not payme_open,
        "cancel": awaiting and not click_inflight,
        "reorder": not awaiting or expired,
    }


def _shipment(order: Order) -> Shipment | None:
    try:
        return order.shipment
    except Shipment.DoesNotExist:
        return None


def order_summary(order: Order, lang: str, now: datetime) -> dict[str, Any]:
    """``OrderSummary``."""
    payment = payment_selectors.pick_display(_payments(order))
    pay_status = payment.status if payment is not None else enums.PaymentStatus.PENDING
    items = sorted(order.items.all(), key=lambda item: item.position)
    shipment = _shipment(order)
    return {
        "id": order.pk,
        "created_at": order.created_at,
        "status": order.status,
        "status_label": status_label(order.status, order.fulfilment, lang),
        "fulfilment": order.fulfilment,
        "total": order.total,
        "item_count": len(items),
        "preview_images": [url for url in (image_url(item.image_url) for item in items) if url][:3],
        "payment": {
            "status": pay_status,
            "status_label": i18n.t(f"label.payment_status.{pay_status}", lang),
            "provider": payment.provider if payment is not None else order.payment_provider,
            "in_progress": payment_in_progress(order, now),
        },
        "pay_by": order.reservation_expires_at if order.status == _S.AWAITING_PAYMENT else None,
        "tracking_number": (shipment.tracking_number or None) if shipment is not None else None,
    }


def _refund_view(order: Order, payment: Payment | None, lang: str) -> dict[str, Any] | None:
    if payment is None:
        return None
    rows = [row for row in payment_selectors.refunds_for_order(order.pk) if row.payment_id == payment.pk]
    if not rows:
        return None
    newest = max(rows, key=lambda row: (row.requested_at, row.id))
    state = CUSTOMER_REFUND_STATE[newest.state]
    return {
        "state": state,
        "state_label": i18n.t(f"label.refund.{state}", lang),
        "amount": newest.amount,
        "requested_at": newest.requested_at,
        "completed_at": newest.completed_at,
    }


def _cashback_view(order: Order) -> dict[str, Any]:
    purchase = loyalty_selectors.purchase_for_order(order.pk)
    earned = purchase.cashback if purchase is not None else 0
    if purchase is None or earned <= 0:
        state = enums.CashbackEarnedState.NONE
    elif purchase.reversed:
        state = enums.CashbackEarnedState.REVERSED
    else:
        state = enums.CashbackEarnedState.CREDITED
    return {"used": order.cashback_used, "earned": earned, "earned_state": state}


def customer_order_detail(order: Order, lang: str, now: datetime | None = None) -> dict[str, Any]:
    """``OrderDetail`` (customer contract §2.8); the caller resolved ownership and the history horizon."""
    instant = now or timeutils.now()
    summary = order_summary(order, lang, instant)
    payment = payment_selectors.pick_display(_payments(order))
    in_progress = summary["payment"]["in_progress"]
    items = []
    for item in sorted(order.items.all(), key=lambda row: row.position):
        name = product_name(item.product, lang) if item.product is not None else ""
        items.append(
            {
                "product_id": item.product_id,
                "name": name or item.name,
                "image_url": image_url(item.image_url),
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "promo": item.promo,
                "line_total": item.line_total,
            }
        )
    delivery = order.fulfilment == enums.FulfilmentMethod.DELIVERY
    shipment = _shipment(order)
    has_parcel = (
        shipment is not None
        and shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED)
        and bool(shipment.tracking_number)
    )
    return {
        **summary,
        "fulfilment_label": i18n.t(f"label.fulfilment.{order.fulfilment}", lang),
        "items": items,
        "items_subtotal": order.items_subtotal,
        "promo_subtotal": order.promo_subtotal,
        "delivery_fee": order.delivery_fee,
        "cashback_used": order.cashback_used,
        "address": order.address_text if delivery and order.address_text else None,
        "address_comment": order.address_comment or None,
        "pickup_address": None if delivery else pickup_address(lang),
        "customer_note": order.customer_note or None,
        "payment": {
            **summary["payment"],
            "amount": payment.amount if payment is not None else order.total,
            "confirmed_at": payment.confirmed_at if payment is not None else None,
            "in_progress_label": i18n.t("label.payment.in_progress", lang) if in_progress else None,
        },
        "refund": _refund_view(order, payment, lang),
        "shipment": (
            {"carrier": "BTS", "tracking_number": shipment.tracking_number, "tracking_url": None}
            if has_parcel and shipment is not None
            else None
        ),
        "cashback": _cashback_view(order),
        "cancellation": (
            {
                "reason": order.cancellation_reason,
                "label": i18n.t(f"label.cancellation.{order.cancellation_reason}", lang),
            }
            if order.status == _S.CANCELLED and order.cancellation_reason
            else None
        ),
        "progress": progress(order, lang),
        "timeline": customer_timeline(order, lang),
        "actions": actions(order, instant),
    }


# --- timeline (customer contract §4.1) and progress (§4.2) -------------------------------------------------


def _entry(event_type: str, at: datetime, title: str, subtitle: str | None) -> dict[str, Any]:
    return {"type": event_type, "at": at, "title": title, "subtitle": subtitle}


def customer_timeline(order: Order, lang: str) -> list[dict[str, Any]]:
    """The customer entries of the order's events, newest first (``at`` desc, ``id`` desc). Hidden and folded
    types never appear; staff names, notes, Billz ids, BTS failures and refund failures never reach the
    app."""
    events = list(OrderEvent.objects.filter(order=order).order_by("-at", "-id"))
    folded_refunds = {
        event.data.get("refund_id")
        for event in events
        if event.type == _E.CANCELLED and event.data.get("refund_id") is not None
    }
    credit = next(
        (int(event.data.get("amount") or 0) for event in events if event.type == _E.CASHBACK_CREDITED), 0
    )
    pickup = order.fulfilment == enums.FulfilmentMethod.PICKUP

    def n(value: Any) -> str:
        return i18n.format_number(int(value or 0), lang)

    def label(key: str, **params: Any) -> str:
        return i18n.t(key, lang, **params)

    entries: list[dict[str, Any]] = []
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        provider = provider_brand(str(data.get("provider") or ""))
        if event.type == _E.ORDER_CREATED:
            entries.append(
                _entry(
                    enums.TimelineType.ORDER_CREATED,
                    event.at,
                    label("label.timeline.order_created"),
                    label(
                        "label.timeline.sub_items_total",
                        item_count=n(data.get("item_count")),
                        total=n(data.get("total")),
                    ),
                )
            )
        elif event.type == _E.PAYMENT_CONFIRMED:
            subtitle = label("label.timeline.sub_paid_via", provider=provider)
            if credit > 0:
                subtitle += " · " + label("label.timeline.sub_cashback", amount=n(credit))
            entries.append(
                _entry(
                    enums.TimelineType.PAYMENT_CONFIRMED,
                    event.at,
                    label("label.timeline.payment_confirmed"),
                    subtitle,
                )
            )
        elif event.type == _E.PAYMENT_FAILED:
            key = (
                "label.timeline.sub_payment_failed_retry"
                if order.status == _S.AWAITING_PAYMENT
                else "label.timeline.sub_provider"
            )
            entries.append(
                _entry(
                    enums.TimelineType.PAYMENT_FAILED,
                    event.at,
                    label("label.timeline.payment_failed"),
                    label(key, provider=provider),
                )
            )
        elif event.type == _E.SHIPMENT_CREATED:
            entries.append(
                _entry(
                    enums.TimelineType.SHIPMENT_CREATED,
                    event.at,
                    label("label.timeline.shipment_created"),
                    label(
                        "label.timeline.sub_tracking", tracking_number=str(data.get("tracking_number") or "")
                    ),
                )
            )
        elif event.type == _E.STATUS_CHANGED and data.get("to") == _S.HANDED_TO_COURIER:
            entries.append(
                _entry(
                    enums.TimelineType.HANDED_TO_COURIER,
                    event.at,
                    label("label.timeline.handed_to_courier"),
                    None,
                )
            )
        elif event.type == _E.STATUS_CHANGED and data.get("to") == _S.DELIVERED:
            title = label("label.timeline.picked_up" if pickup else "label.timeline.delivered")
            entries.append(_entry(enums.TimelineType.DELIVERED, event.at, title, None))
        elif event.type == _E.HANDED_TO_COURIER:
            entries.append(
                _entry(
                    enums.TimelineType.HANDED_TO_COURIER,
                    event.at,
                    label("label.timeline.handed_to_courier"),
                    label("label.timeline.sub_bts"),
                )
            )
        elif event.type == _E.DELIVERED:
            entries.append(
                _entry(
                    enums.TimelineType.DELIVERED,
                    event.at,
                    label("label.timeline.delivered"),
                    label("label.timeline.sub_bts"),
                )
            )
        elif event.type == _E.CANCELLED:
            reason = data.get("reason")
            key = (
                "label.timeline.sub_cancelled_customer"
                if reason == enums.CancellationReason.CUSTOMER_REQUEST
                else "label.timeline.sub_cancelled_admin"
            )
            subtitle = label(key)
            if data.get("refund_id") is not None:
                subtitle += " · " + label("label.timeline.sub_refund_follows")
            entries.append(
                _entry(enums.TimelineType.CANCELLED, event.at, label("label.timeline.cancelled"), subtitle)
            )
        elif event.type == _E.RESERVATION_EXPIRED:
            entries.append(
                _entry(
                    enums.TimelineType.CANCELLED,
                    event.at,
                    label("label.timeline.cancelled"),
                    label("label.timeline.sub_expired"),
                )
            )
        elif event.type == _E.REFUND_REQUESTED and data.get("refund_id") not in folded_refunds:
            entries.append(
                _entry(
                    enums.TimelineType.REFUND_IN_PROGRESS,
                    event.at,
                    label("label.timeline.refund_in_progress"),
                    label("label.timeline.sub_amount", amount=n(data.get("amount"))),
                )
            )
        elif event.type == _E.REFUND_SUCCEEDED:
            entries.append(
                _entry(
                    enums.TimelineType.REFUND_SUCCEEDED,
                    event.at,
                    label("label.timeline.refund_succeeded"),
                    label(
                        "label.timeline.sub_amount_provider", amount=n(data.get("amount")), provider=provider
                    ),
                )
            )
    return entries


def progress(order: Order, lang: str) -> list[dict[str, Any]]:
    """The step bar (customer contract §4.2)."""
    delivery = order.fulfilment == enums.FulfilmentMethod.DELIVERY
    steps: list[tuple[str, datetime | None, bool]] = [
        (enums.ProgressStep.PLACED, order.created_at, True),
        (enums.ProgressStep.PAID, order.paid_at, order.paid_at is not None),
    ]
    if delivery:
        with_courier = order.handed_to_courier_at is not None or order.delivered_at is not None
        steps.append((enums.ProgressStep.WITH_COURIER, order.handed_to_courier_at, with_courier))
        steps.append((enums.ProgressStep.DELIVERED, order.delivered_at, order.delivered_at is not None))
    else:
        steps.append((enums.ProgressStep.PICKED_UP, order.delivered_at, order.delivered_at is not None))
    cancelled = order.status == _S.CANCELLED
    result: list[dict[str, Any]] = []
    current_given = False
    for step, at, reached in steps:
        if reached:
            state = enums.ProgressState.DONE
        elif cancelled:
            state = enums.ProgressState.SKIPPED
        elif not current_given:
            state = enums.ProgressState.CURRENT
            current_given = True
        else:
            state = enums.ProgressState.UPCOMING
        result.append(
            {
                "step": step,
                "label": i18n.t(f"label.progress.{step}", lang),
                "state": state,
                "at": at if reached else None,
            }
        )
    if cancelled:
        result.append(
            {
                "step": enums.ProgressStep.CANCELLED,
                "label": i18n.t("label.progress.cancelled", lang),
                "state": enums.ProgressState.DONE,
                "at": order.cancelled_at,
            }
        )
    return result


# --- fake providers holding open money (customer contract §6.14)
# ------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenMoney:
    open_transactions: int
    open_refunds: int
    order_ids: list[int]


OPEN_MONEY_REFUND_STATES: tuple[str, ...] = (
    enums.RefundState.REQUESTED,
    enums.RefundState.SUBMITTING,
    enums.RefundState.UNKNOWN,
    enums.RefundState.MANUAL_REQUIRED,
    enums.RefundState.FAILED,
)
OPEN_MONEY_MAX_ORDER_IDS = 20


def provider_open_money(provider: str) -> OpenMoney:
    """What a payment provider still holds: ``pending`` payments with an open transaction and refunds not
    ``succeeded`` whose payment is still ``paid``; at most 20 order ids, ascending."""
    open_payments = [
        p for p in Payment.objects.filter(provider=provider, status=enums.PaymentStatus.PENDING) if is_open(p)
    ]
    refunds = list(
        Refund.objects.filter(
            payment__provider=provider,
            payment__status=enums.PaymentStatus.PAID,
            state__in=list(OPEN_MONEY_REFUND_STATES),
        ).values_list("id", "payment__order_id", "state", "payment_id")
    )
    # a failed refund superseded by a newer refund of the same payment is history, not open money
    newest: dict[int, int] = {}
    for refund_id, _order_id, _state, payment_id in refunds:
        newest[payment_id] = max(newest.get(payment_id, 0), refund_id)
    open_refunds = [
        (refund_id, order_id)
        for refund_id, order_id, state, payment_id in refunds
        if state != enums.RefundState.FAILED or newest[payment_id] == refund_id
    ]
    order_ids = sorted({p.order_id for p in open_payments} | {order_id for _, order_id in open_refunds})
    return OpenMoney(
        open_transactions=len(open_payments),
        open_refunds=len(open_refunds),
        order_ids=order_ids[:OPEN_MONEY_MAX_ORDER_IDS],
    )
