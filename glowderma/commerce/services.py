"""Transactional writes of `commerce` — the order orchestrator (design 03 §4.1; contract §2.2, §4).

``commerce`` is the upper module of every order flow: it owns the transaction, takes the locks in the global
order **Order → Payment → LoyaltyAccount** (02 §1.9.2; the synchronous BTS cancel happens right after the
Order lock and before any write), calls the lower modules through their services (``payments``,
``delivery``, ``loyalty``) and is the only writer of ``OrderEvent`` rows (:func:`record_event`). Upward
effects travel through the outbox: ``push.order_status`` (handled by ``notifications``), ``billz.push_sale``,
and — enqueued by the lower modules — ``bts.create_shipment`` / ``refund.submit`` (handled in
``commerce.tasks``).

Every status change happens under ``select_for_update`` of the order row with ``status_changed_at``, the
matching timestamp column and exactly one status-changing event in the same transaction; the transitions are
the data of :mod:`glowderma.commerce.state`.

Errors: request-level refusals raise the contract exceptions of ``core.api.exceptions`` (``NotCancellable``,
``InvalidTransition``, ``Conflict``, ``ProviderUnavailable``, all 409/503) with the contract messages;
:class:`OutOfStock` / :class:`CheckoutError` are the M2 checkout errors; ``OrderNotFound`` is a 404.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from glowderma.accounts.models import Address, Customer, Device, StaffUser
from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import events, selectors, state, topics
from glowderma.commerce.models import (
    EVENT_SOURCES,
    EVENT_STATUS_AFTER,
    Cart,
    CartItem,
    Order,
    OrderEvent,
    OrderItem,
    StockReservation,
)
from glowderma.core import audit, enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import (
    AlreadyPaid,
    AuthFailed,
    CartEmpty,
    CartFull,
    Conflict,
    DeliveryUnavailable,
    InvalidTransition,
    NotCancellable,
    OnlineSalesUnavailable,
    OpenOrderExists,
    PaymentInProgress,
    ProductUnavailable,
    ProviderUnavailable,
    not_cancellable,
)
from glowderma.core.api.exceptions import OutOfStock as OutOfStockError
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click.ports import ClickUnavailable
from glowderma.integrations.click.registry import get_click
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services
from glowderma.payments.models import OPEN_REFUND_STATES, Refund
from glowderma.site_config.models import SiteSettings

MESSAGE_NOT_CANCELLABLE = "Bu buyurtmani bekor qilib bo‘lmaydi."
MESSAGE_BTS_REFUSED = "BTS buyurtmani bekor qilmadi: {reason}."
MESSAGE_INVALID_TRANSITION = "Bu holatga o‘tib bo‘lmaydi."
MESSAGE_RETRY_REFUSED = "BTS ga qayta yuborib bo‘lmaydi."
MESSAGE_RETRY_PENDING = "BTS so‘rovi allaqachon yuborilmoqda."
MESSAGE_REFUND_NOT_RESOLVABLE = "Bu qaytarishni yakunlab bo‘lmaydi."
MESSAGE_REFUND_NOT_RETRYABLE = "Bu qaytarishni qayta yuborib bo‘lmaydi."
MESSAGE_REFUND_OPEN = "Bu to‘lov uchun qaytarish allaqachon jarayonda."
MESSAGE_ORDER_BUSY = "Buyurtma boshqa amal bilan band. Birozdan so‘ng qayta urinib ko‘ring."
MESSAGE_PURCHASE_CONFLICT = "Buyurtma xaridi boshqa mijozga tegishli. Ma’lumotlarni tekshiring."
# How long an admin request waits for the order row (a BTS create holds it across two bounded BTS calls).
ADMIN_LOCK_WAIT = timedelta(seconds=5)
LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL SQLSTATE lock_not_available (lock_timeout expired)
REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 500

_S = enums.OrderStatus
_E = enums.OrderEventType
_SRC = enums.EventSource


class CommerceError(Exception):
    """Base class of the module's typed errors."""


class OrderNotFound(CommerceError):
    pass


class CheckoutError(CommerceError, ValueError):
    """A checkout input the M2 API rejects (unknown product, empty cart, cashback above the subtotal …)."""


class OutOfStock(CommerceError):
    """Problem lines of a checkout (customer contract §2.7.1 row 7, ``OUT_OF_STOCK``): every line that is not
    customer-visible (``unavailable``), has nothing available (``out_of_stock``) or less than requested
    (``insufficient``), as ``{product_id, name, requested, available, status}`` in cart order."""

    def __init__(self, lines: Sequence[Mapping[str, Any]]) -> None:
        self.lines = [dict(line) for line in lines]
        self.product_ids = tuple(sorted(int(line["product_id"]) for line in self.lines))
        super().__init__(f"Out of stock: {list(self.product_ids)}")


class CashbackAboveMax(CommerceError):
    """Row 8: ``cashback_used > cashback_max(items_subtotal, delivery_fee, ∞)``."""

    def __init__(self, maximum: int) -> None:
        super().__init__(f"At most {maximum} so'm of cashback")
        self.max = maximum


class PriceChanged(CommerceError):
    """Row 9: ``expected_total`` differs from the live total (``totals``: total, items_subtotal, delivery_fee,
    cashback_used)."""

    def __init__(self, totals: Mapping[str, int]) -> None:
        super().__init__(f"Expected total differs: {dict(totals)}")
        self.totals = dict(totals)


class InsufficientCashback(CommerceError):
    """Row 10: ``cashback_used > available`` under the LoyaltyAccount lock (``loyalty.create_hold``)."""

    def __init__(self, available: int) -> None:
        super().__init__(f"Only {available} so'm of cashback available")
        self.available = available


class AddressNotFound(CommerceError):
    """The delivery ``address_id`` is not the customer's (row 3)."""


class AddressIncomplete(CommerceError):
    """The delivery address has no BTS region/city (row 3)."""


class ProductNotFound(CommerceError):
    """``POST cart/items/``: unknown product id (``product_id`` ``does_not_exist``)."""


class CartLineNotFound(CommerceError):
    """``PATCH cart/items/{product_id}/`` of a line the cart does not have (404)."""


# --- helpers ------------------------------------------------------------------------------------------------


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and timezone.is_naive(value):
        raise ValueError(f"{name} must be timezone-aware")


def _set_lock_timeout(value: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [value])


def lock_order(order_id: int, *, skip_locked: bool = False, wait: timedelta | None = None) -> Order:
    """The order row ``SELECT … FOR UPDATE OF`` itself — the first lock of every flow.

    ``wait`` bounds the wait for the row (``SET LOCAL lock_timeout`` inside a savepoint, restored right after
    the row is locked): the admin paths pass :data:`ADMIN_LOCK_WAIT` and answer 409 ``conflict``
    (:data:`MESSAGE_ORDER_BUSY`) instead of queueing behind a BTS call without limit.
    """
    qs = Order.objects.select_for_update(of=("self",), skip_locked=skip_locked).filter(pk=order_id)
    if wait is None:
        order = qs.first()
    else:
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('lock_timeout')")
                    row = cursor.fetchone()
                previous = str(row[0]) if row else "0"
                _set_lock_timeout(f"{max(1, int(wait.total_seconds() * 1000))}ms")
                order = qs.first()
                _set_lock_timeout(previous)
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) == LOCK_NOT_AVAILABLE:
                raise Conflict(MESSAGE_ORDER_BUSY) from exc
            raise
    if order is None:
        raise OrderNotFound(f"Order {order_id} does not exist")
    return order


def ledger_instant(customer_id: int, floor: datetime) -> datetime:
    """The instant a live flow posts the customer's ledger entries at: ``max(floor, newest entry's at)``,
    read under the ``LoyaltyAccount`` row lock, which this transaction then keeps until it commits.

    ``post_entry`` refuses an explicit ``at`` older than the account's newest entry (T19), and
    ``record_online_purchase`` / ``reverse_purchase`` require an explicit ``at``. A ``now()`` read before
    that lock (before a BTS call or an Order-lock wait) is older than any entry another transaction posts
    meanwhile (a POS sale, an adjustment, another order's capture), so the live flows take their instant
    here instead, through ``loyalty.services.lock_account`` (callers take it after the Payment lock, so the
    lock order Order → Payment → LoyaltyAccount is kept).
    """
    newest = loyalty_services.lock_account(customer_id)
    return floor if newest is None or newest <= floor else newest


def fake_scenario(order: Order, provider: str) -> dict[str, Any]:
    """``metadata["fake"][provider]`` (design 03 §1.3) — the fake adapters' scenario switches."""
    fake = order.metadata.get("fake") if isinstance(order.metadata, dict) else None
    scenario = fake.get(provider) if isinstance(fake, dict) else None
    result = dict(scenario) if isinstance(scenario, dict) else {}
    if provider == "click" and isinstance(fake, dict) and "refund" in fake and "refund" not in result:
        result["refund"] = fake["refund"]  # the older top-level key of 03 §1.3
    return result


def push_sent(customer_id: int) -> bool:
    """``push_sent`` (plan §4.3): the customer has a pushable device now (the push itself is asynchronous)."""
    return Device.objects.filter(
        customer_id=customer_id,
        is_active=True,
        push_enabled=True,
        customer__status=enums.CustomerStatus.ACTIVE,
    ).exists()


def record_event(
    order: Order,
    event_type: str,
    data: dict[str, Any],
    *,
    source: str | None = None,
    actor_id: int | None = None,
    at: datetime | None = None,
) -> OrderEvent:
    """``rec(...)``: the only ``OrderEvent`` writer; the caller holds the order lock.

    ``status_after`` follows from the type (``status_changed`` → ``data["to"]``); ``source`` defaults to the
    type's only allowed source (``admin`` when an actor is given). ``at`` defaults to ``timeutils.now()`` so
    sibling events of one step share the instant. ``data`` must have exactly the contract keys.
    """
    events.check_data(event_type, data)
    _aware(at, "at")
    if source is None:
        allowed = EVENT_SOURCES[event_type]
        source = _SRC.ADMIN if actor_id is not None and _SRC.ADMIN in allowed else allowed[0]
    if event_type == _E.STATUS_CHANGED:
        status_after = str(data["to"])
    else:
        status_after = EVENT_STATUS_AFTER.get(event_type, ("",))[0]
    return OrderEvent.objects.create(
        order=order,
        type=event_type,
        source=source,
        status_after=status_after,
        actor_id=actor_id if source == _SRC.ADMIN else None,
        data=data,
        at=at or timeutils.now(),
    )


def _event_exists(order_id: int, event_type: str, data: Mapping[str, Any]) -> bool:
    keys = events.DEDUPE_KEYS.get(event_type)
    if keys is None:
        return False
    lookup = {f"data__{key}": data[key] for key in keys}
    return OrderEvent.objects.filter(order_id=order_id, type=event_type, **lookup).exists()


def record_event_once(
    order: Order, event_type: str, data: dict[str, Any], **kwargs: Any
) -> OrderEvent | None:
    """:func:`record_event` unless the same event (type + discriminating key) exists (at-least-once)."""
    if _event_exists(order.pk, event_type, data):
        return None
    return record_event(order, event_type, data, **kwargs)


def _enqueue_push(order: Order, status: str, event: OrderEvent) -> None:
    """``push.order_status`` (03 §5): one push per order per status; the handler is ``notifications``'."""
    if status not in state.PUSHABLE_STATUSES:
        return
    outbox.enqueue(
        topics.PUSH_ORDER_STATUS,
        {"order_id": order.pk, "status": status, "event_id": event.pk, "customer_id": order.customer_id},
        dedupe_key=topics.push_order_status_key(order.pk, status),
    )


def _set_status(order: Order, status: str, at: datetime, **extra: Any) -> None:
    order.status = status
    order.status_changed_at = at
    field = state.STATUS_TIMESTAMP_FIELD.get(status)
    if field is not None:
        setattr(order, field, at)
    for name, value in extra.items():
        setattr(order, name, value)
    order.save()


def _audit(action: str, order: Order, actor_id: int | None, request: Any, after: dict[str, Any]) -> None:
    actor = None if actor_id is None else StaffUser.objects.get(pk=actor_id)
    audit.record(
        action=action, entity_type="order", entity_id=str(order.pk), actor=actor, request=request, after=after
    )


# --- checkout (row 1) ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Line:
    product: Product
    quantity: int


def _first_image_path(product: Product) -> str:
    """The media storage path of the product's first image (the snapshot; rendered absolute by the API)."""
    image = product.images.order_by("position", "id").first()
    return (image.file.name or "") if image is not None and image.file else ""


def _problem_lines(
    lines: Sequence[_Line], available: Mapping[int, int], visible: set[int] | None
) -> list[dict[str, Any]]:
    problems = []
    for line in lines:
        product = line.product
        stock = max(0, available.get(product.pk, 0))
        if visible is not None:
            shown = product.pk in visible
        else:
            shown = product.status == enums.ProductStatus.ACTIVE
        if not shown:
            status = enums.CartLineStatus.UNAVAILABLE
        elif stock <= 0:
            status = enums.CartLineStatus.OUT_OF_STOCK
        elif stock < line.quantity:
            status = enums.CartLineStatus.INSUFFICIENT
        else:
            continue
        problems.append(
            {
                "product_id": product.pk,
                "name": product.name_uz or product.billz_name or product.billz_id,
                "requested": line.quantity,
                "available": stock,
                "status": status,
            }
        )
    return problems


def create_order(
    customer_id: int,
    items: Sequence[tuple[int, int]],
    fulfilment: str,
    *,
    payment_provider: str,
    channel: str,
    delivery_fee: int,
    address_id: int | None = None,
    address_text: str = "",
    region_code: str = "",
    city_code: str = "",
    address_comment: str = "",
    cashback_used: int = 0,
    expected_total: int | None = None,
    min_payable: int = 0,
    visible_only: bool = False,
    customer_note: str = "",
    at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    order_id: int | None = None,
    billz_order_uuid: UUID | None = None,
    enforce_stock: bool = True,
) -> int:
    """Checkout (03 §4.1 row 1; customer contract §2.7.1 rows 7–10): snapshot the lines, reserve stock, hold
    cashback → ``awaiting_payment``.

    ``items`` are ``(product_id, quantity)`` in cart order (one line per product). ``delivery_fee`` is the BTS
    quote the **caller** asked before any row lock (``0`` for pickup; the seeder passes its fee), so no HTTP
    call runs under the ``ProductStock`` locks. Evaluation order (the first failure raises a typed error): the
    address (:class:`AddressNotFound` / :class:`AddressIncomplete`); under the ``ProductStock`` row locks
    every line visible (``catalog.selectors.customer_visible_products()`` when ``visible_only``, else
    ``active``) and available (:class:`OutOfStock` with every problem line); ``cashback_used ≤
    cashback_max(subtotal, fee, ∞)`` with ``min_payable`` (:class:`CashbackAboveMax`); ``expected_total``
    (:class:`PriceChanged`); then the rows and the hold (:class:`InsufficientCashback` from ``create_hold``;
    everything rolls back). ``enforce_stock=False`` is for the demo seeder only, whose orders are replayed at
    historical instants against today's Billz stock. ``order_id``/``billz_order_uuid`` pin design ids (seed
    only). Writes the ``Order`` (with ``customer_note``), items, reservations, the hold, the ``pending``
    payment, the delivery shipment and the events ``order_created`` + ``reservation_created``; no outbox, no
    audit. Returns the id.
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    if fulfilment not in enums.FulfilmentMethod.values:
        raise CheckoutError(f"Unknown fulfilment {fulfilment!r}")
    if channel not in enums.OrderChannel.values:
        raise CheckoutError(f"Unknown channel {channel!r}")
    if payment_provider not in enums.PaymentProvider.values:
        raise CheckoutError(f"Unknown payment provider {payment_provider!r}")
    if not items:
        raise CheckoutError("An order needs at least one line")
    product_ids = [product_id for product_id, _ in items]
    if len(set(product_ids)) != len(product_ids):
        raise CheckoutError("One line per product")
    if any(quantity < 1 for _, quantity in items):
        raise CheckoutError("Quantities are positive")
    if cashback_used < 0:
        raise CheckoutError("cashback_used must not be negative")
    delivery = fulfilment == enums.FulfilmentMethod.DELIVERY
    if delivery_fee < 0 or (not delivery and delivery_fee != 0):
        raise CheckoutError("The delivery fee is a non-negative quote, 0 for pickup")
    meta = dict(metadata or {})
    with transaction.atomic():
        customer = Customer.objects.filter(pk=customer_id).first()
        if customer is None or customer.status != enums.CustomerStatus.ACTIVE:
            raise CheckoutError(f"Customer {customer_id} cannot order")
        products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)}
        missing = set(product_ids) - set(products)
        if missing:
            raise CheckoutError(f"Unknown products {sorted(missing)}")
        lines = [_Line(products[product_id], quantity) for product_id, quantity in items]
        address = {"text": "", "region_code": "", "city_code": "", "comment": ""}
        if delivery:
            if address_id is not None:
                saved = Address.objects.filter(pk=address_id, customer_id=customer_id).first()
                if saved is None:
                    raise AddressNotFound(f"Unknown address {address_id}")
                address = {
                    "text": saved.text,
                    "region_code": saved.region_code,
                    "city_code": saved.city_code,
                    "comment": saved.comment,
                }
            else:
                address = {
                    "text": address_text,
                    "region_code": region_code,
                    "city_code": city_code,
                    "comment": address_comment,
                }
            if not address["text"]:
                raise AddressNotFound("A delivery order needs an address")
            if not address["region_code"] or not address["city_code"]:
                raise AddressIncomplete("A delivery address needs the BTS region and city")
        # the caller's BTS quote ran before any row lock; the stock rows are locked only from here to commit
        if enforce_stock:
            list(
                ProductStock.objects.select_for_update(of=("self",))
                .filter(product_id__in=product_ids)
                .order_by("product_id")
            )
            visible = (
                set(
                    catalog_selectors.customer_visible_products()
                    .filter(pk__in=product_ids)
                    .values_list("pk", flat=True)
                )
                if visible_only
                else None
            )
            problems = _problem_lines(lines, selectors.available_stock(product_ids), visible)
            if problems:
                raise OutOfStock(problems)
        # rows 8 and 9 use the prices read at the start of this transaction (the ProductStock locks do not
        # serialise price edits, so a price may be milliseconds old; the customer confirmed expected_total)
        items_subtotal = sum(line.product.price * line.quantity for line in lines)
        promo_subtotal = sum(line.product.price * line.quantity for line in lines if line.product.promo)
        maximum = max(0, min(items_subtotal, items_subtotal + delivery_fee - min_payable))
        if cashback_used > maximum:
            raise CashbackAboveMax(maximum)
        total = items_subtotal + delivery_fee - cashback_used
        if expected_total is not None and expected_total != total:
            raise PriceChanged(
                {
                    "total": total,
                    "items_subtotal": items_subtotal,
                    "delivery_fee": delivery_fee,
                    "cashback_used": cashback_used,
                }
            )
        if total <= 0:
            raise CheckoutError("The order total must be positive")
        minutes = SiteSettings.load().reservation_minutes
        expires_at = instant + timedelta(minutes=minutes)
        if billz_order_uuid is not None:
            meta.setdefault("billz_order_uuid", str(billz_order_uuid))
        order = Order.objects.create(
            **({"id": order_id} if order_id is not None else {}),
            customer=customer,
            channel=channel,
            fulfilment=fulfilment,
            status=_S.AWAITING_PAYMENT,
            address_text=address["text"],
            address_region_code=address["region_code"],
            address_city_code=address["city_code"],
            address_comment=address["comment"],
            recipient_name=customer.name,
            recipient_phone=customer.phone,
            customer_note=customer_note,
            items_subtotal=items_subtotal,
            promo_subtotal=promo_subtotal,
            delivery_fee=delivery_fee,
            cashback_used=cashback_used,
            total=total,
            payment_provider=payment_provider,
            reservation_minutes=minutes,
            reservation_expires_at=expires_at,
            status_changed_at=instant,
            metadata=meta,
            created_at=instant,
        )
        OrderItem.objects.bulk_create(
            OrderItem(
                order=order,
                position=position,
                product=line.product,
                name=line.product.name_uz or line.product.billz_name or line.product.billz_id,
                billz_id=line.product.billz_id,
                image_url=_first_image_path(line.product),
                quantity=line.quantity,
                unit_price=line.product.price,
                promo=line.product.promo,
                line_total=line.product.price * line.quantity,
            )
            for position, line in enumerate(lines)
        )
        StockReservation.objects.bulk_create(
            StockReservation(
                order=order,
                product=line.product,
                quantity=line.quantity,
                expires_at=expires_at,
                created_at=instant,
                updated_at=instant,
            )
            for line in lines
        )
        if cashback_used > 0:
            try:
                loyalty_services.create_hold(customer.pk, order.pk, cashback_used, expires_at)
            except loyalty_services.InsufficientBalance as exc:
                raise InsufficientCashback(exc.available) from exc
        payment_services.create_payment(order.pk, payment_provider, total)
        if delivery:
            delivery_services.ensure_shipment(order.pk)
        record_event(
            order,
            _E.ORDER_CREATED,
            {"channel": channel, "item_count": len(lines), "total": total},
            at=instant,
        )
        record_event(
            order,
            _E.RESERVATION_CREATED,
            {"item_count": len(lines), "minutes": minutes, "expires_at": timeutils.isoformat(expires_at)},
            at=instant,
        )
        return order.pk


# --- payment (row 2) ----------------------------------------------------------------------------------------


def mark_paid(
    order_id: int,
    payment_id: int,
    provider: str,
    provider_txn_id: str,
    at: datetime | None = None,
    *,
    raw: dict[str, Any] | None = None,
) -> None:
    """An honoured provider capture (Click Complete / Payme ``PerformTransaction``; the seed).

    Names the payment explicitly (customer contract §2.13.0 step 4): under Order → Payment → Account,
    ``payment_id`` must be the order's active ``pending`` payment of ``provider``; it becomes ``paid``
    (``payments.services.confirm_payment``, ``raw`` merged into ``provider_state``) and
    :func:`confirm_payment_effects` runs in the same transaction. Anything else raises ``InvalidTransition``
    (the callers turn a capture they cannot honour into :func:`record_late_capture`). Idempotent for the same
    transaction of an already paid order.
    """
    _aware(at, "at")
    with transaction.atomic():
        order = lock_order(order_id)
        instant = at or timeutils.now()  # after the Order lock
        payment = payment_selectors.get_payment(payment_id)
        if payment is None or payment.order_id != order_id or payment.provider != provider:
            raise InvalidTransition(f"Payment {payment_id} is not a {provider} payment of order {order_id}")
        if payment.status == enums.PaymentStatus.PAID and not payment.late_capture:
            if payment.transaction_id == provider_txn_id and order.status != _S.AWAITING_PAYMENT:
                return
        if order.status != _S.AWAITING_PAYMENT:
            raise InvalidTransition(f"Order {order_id} is {order.status}, not awaiting payment")
        pending = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
        if pending is None or pending.id != payment_id:
            raise InvalidTransition(f"Payment {payment_id} is not the pending payment of order {order_id}")
        payment_services.confirm_payment(payment_id, provider_txn_id, instant, raw)
        confirm_payment_effects(order_id, payment_id, at, floor=instant)


def confirm_payment_effects(
    order_id: int, payment_id: int, at: datetime | None = None, *, floor: datetime | None = None
) -> None:
    """After-payment orchestration (03 §4.1 row 2) for an order whose payment is already ``paid``.

    Reservations ``active → converted`` (Billz owns ``ProductStock``; converted units count against
    availability until the next Billz sync, 03 §1.6), the cashback hold is captured (debit), the purchase and
    its credit are recorded under the policy active at ``at``, status ``processing``; events
    ``payment_confirmed`` (+ ``cashback_credited`` when the credit is positive); outbox ``billz.push_sale``
    (the Billz order UUID is persisted on the purchase first), ``bts.create_shipment`` for delivery orders
    and ``push.order_status processing``. No audit (system).

    Without an explicit ``at`` (every live path) the instant of the step is :func:`ledger_instant` — read
    under the Order → Payment → LoyaltyAccount locks, never before them (``floor`` is the caller's instant,
    e.g. the payment's ``confirmed_at``). An explicit ``at`` (the seeder) is used as given.
    """
    _aware(at, "at")
    _aware(floor, "floor")
    with transaction.atomic():
        order = lock_order(order_id)
        payment = payment_selectors.get_payment(payment_id)
        if payment is None or payment.order_id != order_id:
            raise InvalidTransition(f"Payment {payment_id} does not belong to order {order_id}")
        if payment.status != enums.PaymentStatus.PAID:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not paid")
        if order.status != _S.AWAITING_PAYMENT:
            if OrderEvent.objects.filter(order=order, type=_E.PAYMENT_CONFIRMED).exists():
                return  # replay of an applied capture
            raise InvalidTransition(f"Order {order_id} is {order.status}, not awaiting payment")
        instant = at or ledger_instant(order.customer_id, floor or timeutils.now())
        converted = StockReservation.objects.filter(order=order, state=StockReservation.State.ACTIVE).update(
            state=StockReservation.State.CONVERTED, converted_at=instant, updated_at=instant
        )
        loyalty_services.capture_hold(order.pk, at=instant)
        items = [
            loyalty_services.PurchaseLine(
                name=item.name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
                promo=item.promo,
                product_id=item.product_id,
                billz_product_id=item.billz_id,
            )
            for item in order.items.order_by("position")
        ]
        raw_uuid = order.metadata.get("billz_order_uuid") if isinstance(order.metadata, dict) else None
        purchase = loyalty_services.record_online_purchase(
            order.customer_id,
            order.pk,
            order.items_subtotal,
            order.promo_subtotal,
            order.cashback_used,
            instant,
            items=items,
            billz_order_uuid=UUID(str(raw_uuid)) if raw_uuid else None,
        )
        _set_status(order, _S.PROCESSING, instant)
        confirmed = record_event(
            order,
            _E.PAYMENT_CONFIRMED,
            {
                "provider": payment.provider,
                "transaction_id": payment.transaction_id,
                "amount": payment.amount,
                "reservation_converted": converted > 0,
            },
            at=instant,
        )
        if purchase.cashback_amount > 0 and purchase.credited_entry_id is not None:
            record_event(
                order,
                _E.CASHBACK_CREDITED,
                {
                    "ledger_entry_id": purchase.credited_entry_id,
                    "amount": purchase.cashback_amount,
                    "base": purchase.base,
                    "percent": None if purchase.percent is None else f"{purchase.percent:.2f}",
                    "policy_version": purchase.policy_version,
                },
                at=instant,
            )
        if purchase.billz_order_uuid is not None:
            outbox.enqueue(
                topics.BILLZ_PUSH_SALE,
                {
                    "purchase_id": purchase.purchase_id,
                    "order_id": order.pk,
                    "billz_order_uuid": str(purchase.billz_order_uuid),
                },
                dedupe_key=topics.billz_push_sale_key(str(purchase.billz_order_uuid)),
            )
        if order.fulfilment == enums.FulfilmentMethod.DELIVERY:
            delivery_services.ensure_shipment(order.pk)
            delivery_services.request_bts_create(order.pk, manual=False, actor_id=None)
        _enqueue_push(order, _S.PROCESSING, confirmed)


def fail_payment_effects(
    order_id: int,
    payment_id: int,
    error_code: str | None,
    at: datetime | None = None,
    *,
    provider_txn_id: str | None = None,
) -> bool:
    """A provider failure callback (03 §4.1 row 16; Click Complete with ``error < 0``, Payme
    ``CancelTransaction`` of a state-1 transaction) for **that** payment: it fails and ``payment_failed`` is
    recorded; the order stays payable until expiry. ``False`` when the payment is failed already (nothing
    written); a payment of another order, or one that is not ``pending``, raises ``InvalidTransition``."""
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        order = lock_order(order_id)
        payment = payment_selectors.get_payment(payment_id)
        if payment is None or payment.order_id != order_id:
            raise InvalidTransition(f"Payment {payment_id} does not belong to order {order_id}")
        if payment.status == enums.PaymentStatus.FAILED:
            return False
        if payment.status != enums.PaymentStatus.PENDING:
            raise InvalidTransition(f"Payment {payment_id} is {payment.status}, not pending")
        payment_services.fail_payment(payment_id, error_code or "failed", instant)
        record_event(
            order,
            _E.PAYMENT_FAILED,
            {
                "provider": payment.provider,
                "transaction_id": provider_txn_id or payment.transaction_id,
                "error_code": error_code,
            },
            at=instant,
        )
        return True


# --- reservation expiry (row 3) -----------------------------------------------------------------------------


def _in_grace(order: Order, payment: payment_selectors.PaymentRow, now: datetime) -> bool:
    expires = order.reservation_expires_at
    attached = selectors.attached_at(payment)
    if expires is None or attached is None or attached >= expires or not selectors.is_open(payment):
        return False
    return now < expires + timedelta(seconds=settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS)


def expire_reservation(order_id: int, at: datetime | None = None, *, skip_locked: bool = False) -> bool:
    """The reservation of an unpaid order ran out (03 §4.1 row 3): ``cancelled`` / ``reservation_expired``.

    Re-checked under the Order lock (a capture may have won): ``awaiting_payment`` and
    ``reservation_expires_at ≤ now``. An order whose pending payment has an **open transaction** (never "in
    flight": Click is in flight 120 s but open until its Complete) attached before ``reservation_expires_at``
    is skipped until ``reservation_expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS`` (customer contract §2.7.2).
    The pending payment fails silently (unless it is a Payme state-1 transaction, P-2), reservations are
    released, the hold is released (``reservation_expired``); event ``reservation_expired
    {released_item_count, payment_failed}``; no push, no audit. Returns whether the order was expired
    (``False`` = nothing to do, or locked by another transaction with ``skip_locked``).
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        try:
            order = lock_order(order_id, skip_locked=skip_locked)
        except OrderNotFound:
            if skip_locked:
                return False
            raise
        if order.status != _S.AWAITING_PAYMENT or (
            order.reservation_expires_at is not None and order.reservation_expires_at > instant
        ):
            return False
        pending = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
        if pending is not None and _in_grace(order, pending, instant):
            return False  # an open transaction attached in time may still complete (customer contract §2.7.2)
        payment_failed = False
        if pending is not None:
            result = payment_services.fail_payment(pending.id, "expired", instant, unilateral=True)
            payment_failed = result.changed
        released = StockReservation.objects.filter(order=order, state=StockReservation.State.ACTIVE).update(
            state=StockReservation.State.RELEASED,
            released_at=instant,
            release_reason=StockReservation.ReleaseReason.EXPIRED,
            updated_at=instant,
        )
        loyalty_services.release_hold(order.pk, "reservation_expired", at=instant)
        _set_status(
            order, _S.CANCELLED, instant, cancellation_reason=enums.CancellationReason.RESERVATION_EXPIRED
        )
        record_event(
            order,
            _E.RESERVATION_EXPIRED,
            {"released_item_count": released, "payment_failed": payment_failed},
            at=instant,
        )
        return True


def expire_due_reservations(now: datetime | None = None) -> list[int]:
    """Beat ``commerce.expire_reservations`` (1 min): expire every due unpaid order, one transaction each,
    skipping orders another transaction holds (a capture in progress). Returns the expired order ids."""
    _aware(now, "now")
    instant = now or timeutils.now()
    due = list(
        Order.objects.filter(status=_S.AWAITING_PAYMENT, reservation_expires_at__lte=instant)
        .order_by("reservation_expires_at", "id")
        .values_list("id", flat=True)
    )
    return [order_id for order_id in due if expire_reservation(order_id, instant, skip_locked=True)]


# --- admin cancel (rows 4, 6, 7) ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancelResult:
    order_id: int
    refund_id: int | None
    cashback_reversed: bool
    stock_released: bool
    bts_cancelled: bool


def clean_reason(reason: str) -> str:
    text = reason.strip()
    if not REASON_MIN_LENGTH <= len(text) <= REASON_MAX_LENGTH:
        raise ValueError(f"reason must be {REASON_MIN_LENGTH}..{REASON_MAX_LENGTH} characters")
    return text


def _announce_billz_refund(order_id: int, purchase_id: int) -> None:
    """A cancelled order whose Billz sale exists (paid, or a draft a push may still pay): Billz has no refund
    API (stub ``refund_sale``), so once the cancel commits an ERROR line names the sale staff refund — or the
    draft they delete — in Billz by hand (review A S5). A push still running stops before paying
    (``PurchaseJournal.before_payment``) or logs ``billz.sale_of_cancelled_order`` after it."""
    info = loyalty_selectors.billz_push_info(purchase_id)
    if info is None or not info.billz_sale_id:
        return
    sale_id, pushed = info.billz_sale_id, info.billz_pushed_at is not None
    transaction.on_commit(
        lambda: logger.error(
            "billz.refund_by_hand order_id=%s billz_order_id=%s pushed=%s: the order was cancelled; "
            "refund the Billz sale (or delete its draft) in Billz by hand",
            order_id,
            sale_id,
            pushed,
        )
    )


def cancel_order(
    order_id: int, reason: str, actor_id: int, *, at: datetime | None = None, request: Any = None
) -> CancelResult:
    """``POST orders/{id}/cancel/`` (contract §2.2 table; 03 §4.1 rows 4/6/7).

    * ``awaiting_payment`` → the pending payment fails (Payme state 1 excepted), reservations and the hold
      are released;
    * ``processing`` / ``handed_to_courier`` → a BTS parcel that exists is cancelled **first**, synchronously
      (refusal → 409 ``not_cancellable`` with BTS's reason; transport failure → 503 ``provider_unavailable``;
      nothing is written before the answer, and every check that can refuse runs before the call; BTS
      answers ``ok`` again for a parcel it already cancelled, so a cancel rolled back after BTS agreed is
      simply repeated); then a Refund (Click ``requested`` + outbox, Payme
      ``manual_required``; the payment stays ``paid``) and one reversal per unreversed debit/credit;
    * ``delivered`` / ``cancelled`` → 409 ``not_cancellable``.

    Effects: ``cancelled`` (reason ``admin``, note, ``cancelled_by``), events ``cancelled`` (+
    ``refund_requested``), push ``cancelled``, audit ``order.cancelled``. The Order lock is awaited at most
    :data:`ADMIN_LOCK_WAIT` (409 ``conflict``); the reversal instant is :func:`ledger_instant`.
    """
    _aware(at, "at")
    note = clean_reason(reason)
    actor = StaffUser.objects.get(pk=actor_id)
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        if not state.can_cancel(order.status):
            raise NotCancellable(MESSAGE_NOT_CANCELLABLE)
        instant = at or timeutils.now()
        bts_cancelled = False
        paid = None
        purchase = None
        if order.status in (_S.PROCESSING, _S.HANDED_TO_COURIER):
            # everything that can refuse runs before the BTS cancel, the only effect outside the database
            paid = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PAID)
            purchase = loyalty_selectors.purchase_for_order(order.pk)
            shipment = delivery_selectors.shipment_info(order.pk)
            has_parcel = shipment is not None and shipment.state == Shipment.State.CREATED
            if order.status == _S.HANDED_TO_COURIER and not has_parcel:
                raise NotCancellable(MESSAGE_NOT_CANCELLABLE)
            if has_parcel:
                try:
                    outcome = delivery_services.cancel(
                        order.pk, scenario=fake_scenario(order, "bts"), at=instant
                    )
                except BtsUnavailable as exc:
                    raise ProviderUnavailable() from exc
                if not outcome.ok:
                    raise NotCancellable(
                        MESSAGE_BTS_REFUSED.format(reason=outcome.reason or "posilka kuryerda")
                    )
                bts_cancelled = True
        refund = None
        cashback_reversed = False
        stock_released = False
        if order.status == _S.AWAITING_PAYMENT:
            pending = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PENDING)
            if pending is not None:
                payment_services.fail_payment(pending.id, "cancelled", instant, unilateral=True)
            stock_released = (
                StockReservation.objects.filter(order=order, state=StockReservation.State.ACTIVE).update(
                    state=StockReservation.State.RELEASED,
                    released_at=instant,
                    release_reason=StockReservation.ReleaseReason.CANCELLED,
                    updated_at=instant,
                )
                > 0
            )
            loyalty_services.release_hold(order.pk, "order_cancelled", at=instant)
        else:
            if paid is not None:
                refund = payment_services.create_refund(
                    paid.id,
                    paid.amount,
                    actor_id,
                    at=instant,
                    request=request,
                    manual=refund_manual_reason(paid) is not None,
                )
            if purchase is not None:
                # read under the LoyaltyAccount lock, after the BTS call (T19: never an older instant)
                instant = at or ledger_instant(order.customer_id, instant)
                reversal = loyalty_services.reverse_purchase(purchase.id, instant, actor_id)
                cashback_reversed = len(reversal.reversal_entry_ids) > 0
                _announce_billz_refund(order.pk, purchase.id)
        _set_status(
            order,
            _S.CANCELLED,
            instant,
            cancellation_reason=enums.CancellationReason.ADMIN,
            cancellation_note=note,
            cancelled_by_id=actor_id,
        )
        refund_id = refund.refund_id if refund is not None else None
        cancelled = record_event(
            order,
            _E.CANCELLED,
            {
                "reason": enums.CancellationReason.ADMIN,
                "note": note,
                "refund_id": refund_id,
                "cashback_reversed": cashback_reversed,
                "stock_released": stock_released,
                "bts_cancelled": bts_cancelled,
                "push_sent": push_sent(order.customer_id),
            },
            actor_id=actor_id,
            at=instant,
        )
        if refund is not None and paid is not None:
            record_event_once(
                order, _E.REFUND_REQUESTED, refund_requested_data(refund, paid), actor_id=actor_id, at=instant
            )
        _enqueue_push(order, _S.CANCELLED, cancelled)
        audit.record(
            action=state.AUDIT_ORDER_CANCELLED,
            entity_type="order",
            entity_id=str(order.pk),
            actor=actor,
            request=request,
            after={
                "reason": enums.CancellationReason.ADMIN,
                "note": note,
                "refund_id": refund_id,
                "bts_cancelled": bts_cancelled,
            },
        )
        return CancelResult(order.pk, refund_id, cashback_reversed, stock_released, bts_cancelled)


# --- manual transitions (rows 8–10) -------------------------------------------------------------------------


def transition(
    order_id: int, status: str, actor_id: int, *, at: datetime | None = None, request: Any = None
) -> None:
    """``POST orders/{id}/transition/``: one of ``available_actions.transitions`` (state.py rows 8–10), else
    409 ``invalid_transition``. Event ``status_changed``, push, audit ``order.status_changed``."""
    _aware(at, "at")
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        if status not in state.manual_transitions(order.status, order.fulfilment):
            raise InvalidTransition(MESSAGE_INVALID_TRANSITION)
        instant = at or timeutils.now()
        before = order.status
        _set_status(order, status, instant)
        changed = record_event(
            order,
            _E.STATUS_CHANGED,
            {"from": before, "to": status, "push_sent": push_sent(order.customer_id)},
            actor_id=actor_id,
            at=instant,
        )
        _enqueue_push(order, status, changed)
        _audit(state.AUDIT_ORDER_STATUS_CHANGED, order, actor_id, request, {"from": before, "to": status})


# --- BTS status (rows 11–12) --------------------------------------------------------------------------------


def apply_bts_status(
    order_id: int,
    bts_status: str,
    via: str,
    *,
    payload: Mapping[str, Any] | None = None,
    at: datetime | None = None,
) -> bool:
    """A BTS webhook / poll status (03 §4.1 rows 11/12): recorded on the shipment; ``200`` moves a
    ``processing`` order to ``handed_to_courier``, ``300`` a processing/handed order to ``delivered`` (event
    source ``bts``, push). A status for a terminal order, or one already applied, only records the shipment
    side. Returns whether the order status changed."""
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        order = lock_order(order_id)
        result = delivery_services.apply_status(
            order.pk, bts_status=bts_status, via=via, payload=payload, at=instant
        )
        target = result.target_status
        if target is None or not result.changed:
            return False
        transition_row = state.find_transition(
            order.status, target, state.Trigger.BTS_STATUS, fulfilment=order.fulfilment
        )
        if transition_row is None:
            return False
        _set_status(order, target, instant)
        event = record_event(
            order,
            transition_row.event_type,
            {"via": via, "bts_status": result.bts_status, "push_sent": push_sent(order.customer_id)},
            source=_SRC.BTS,
            at=instant,
        )
        _enqueue_push(order, target, event)
        return True


# --- retry shipment (row 13) --------------------------------------------------------------------------------


def retry_shipment(order_id: int, actor_id: int, *, request: Any = None) -> None:
    """``POST orders/{id}/retry-shipment/``: delivery ∧ ``processing`` ∧ shipment ``failed`` (else 409
    ``invalid_transition``), not ``pending`` (else 409 ``conflict``); enqueues a manual
    ``bts.create_shipment`` and audits ``order.shipment_retry_requested``. Both checks run under the Order
    lock, so two concurrent retries cannot both enqueue."""
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        shipment = delivery_selectors.shipment_info(order.pk)
        open_delivery = order.fulfilment == enums.FulfilmentMethod.DELIVERY and order.status == _S.PROCESSING
        if shipment is None or not open_delivery or shipment.state != Shipment.State.FAILED:
            raise InvalidTransition(MESSAGE_RETRY_REFUSED)
        if shipment.pending:
            raise Conflict(MESSAGE_RETRY_PENDING)
        message_id = delivery_services.request_bts_create(order.pk, manual=True, actor_id=actor_id)
        _audit(
            state.AUDIT_SHIPMENT_RETRY_REQUESTED,
            order,
            actor_id,
            request,
            {"request_no": shipment.create_requests + 1, "outbox_message_id": message_id},
        )


# --- refunds (handlers, Payme confirmation, staff resolve/retry) --------------------------------------------

MESSAGE_REFUND_STILL_PAID = (
    "Click to‘lovni qaytarmagan. Avval mijozga qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan "
    "yakunlang yoki qaytarishni qayta urinib ko‘ring."
)
MESSAGE_REFUND_CLICK_FAKE = (
    "Click sinov rejimida: to‘lov holatini tekshirib bo‘lmaydi. Click ulanganidan keyin yakunlang."
)
MANUAL_REASON_PAYME = "payme"
MANUAL_REASON_NO_PAYDOC = "no_paydoc"
MANUAL_REASON_FAKE_MODE = "fake_mode"


def refund_manual_reason(payment: payment_selectors.PaymentRow) -> str | None:
    """Why a new refund of ``payment`` is ``manual_required`` (admin §4 ``refund_requested.manual_reason``):
    ``payme`` (Payme refunds are cabinet refunds), ``no_paydoc`` (a Click payment without ``click_paydoc_id``
    was never sent to Click with another id), ``fake_mode`` (the Click port is fake under the production
    settings: the fake would report a reversal that moved no money); ``None`` = submitted to Click."""
    if payment.provider == enums.PaymentProvider.PAYME:
        return MANUAL_REASON_PAYME
    if not payment_selectors.click_paydoc_id(payment.provider_state):
        return MANUAL_REASON_NO_PAYDOC
    if integration_modes.callbacks_refused("click"):
        return MANUAL_REASON_FAKE_MODE
    return None


def refund_requested_data(
    refund: payment_services.RefundResult, payment: payment_selectors.PaymentRow
) -> dict[str, Any]:
    """``refund_requested`` data with ``manual_reason`` (null unless the refund is ``manual_required``)."""
    manual = refund.state == enums.RefundState.MANUAL_REQUIRED
    return {
        "refund_id": refund.refund_id,
        "amount": refund.amount,
        "provider": refund.provider,
        "manual": manual,
        "manual_reason": (refund_manual_reason(payment) or MANUAL_REASON_PAYME) if manual else None,
    }


def _refund_event_data(refund: payment_services.RefundResult) -> dict[str, Any]:
    return {"refund_id": refund.refund_id, "amount": refund.amount, "provider": refund.provider}


def record_refund_outcome(order: Order, refund: payment_services.RefundResult, *, at: datetime) -> None:
    """``refund_succeeded`` / ``refund_failed`` once per refund (the caller holds the order lock)."""
    if refund.state == enums.RefundState.SUCCEEDED:
        record_event_once(order, _E.REFUND_SUCCEEDED, _refund_event_data(refund), at=at)
    elif refund.state in (enums.RefundState.FAILED, enums.RefundState.UNKNOWN):
        record_event_once(
            order,
            _E.REFUND_FAILED,
            {
                **_refund_event_data(refund),
                "state": refund.state,
                "error_code": refund.error_code or None,
            },
            at=at,
        )


def _locked_refund_row(refund_id: int) -> tuple[Order, payment_selectors.RefundRow]:
    """The refund re-read under its order's lock (the state checks of resolve/retry happen here)."""
    row = payment_selectors.get_refund(refund_id)
    if row is None:
        raise payment_services.RefundNotFound(f"Refund {refund_id} does not exist")
    order = lock_order(row.order_id, wait=ADMIN_LOCK_WAIT)
    locked = payment_selectors.get_refund(refund_id)
    if locked is None:  # pragma: no cover - refunds are never deleted
        raise payment_services.RefundNotFound(f"Refund {refund_id} does not exist")
    return order, locked


def _click_status_for_resolve(row: payment_selectors.RefundRow, manual_payout: bool) -> bool:
    """The fresh Click status check of a ``failed`` Click refund's resolve (customer contract §2.13.0), run
    **outside any row lock**: ``True`` when Click reversed the payment (closed without a payout); a payout
    without ``manual_payout`` → 409 ``MESSAGE_REFUND_STILL_PAID``; Click down → 503; fake Click under the
    production settings → 409 ``MESSAGE_REFUND_CLICK_FAKE``."""
    if integration_modes.callbacks_refused("click"):
        raise InvalidTransition(MESSAGE_REFUND_CLICK_FAKE)
    if not row.provider_payment_ref:
        if not manual_payout:
            raise InvalidTransition(MESSAGE_REFUND_STILL_PAID)
        return False
    order = Order.objects.get(pk=row.order_id)
    try:
        status = get_click(fake_scenario(order, "click")).payment_status(row.provider_payment_ref)
    except ClickUnavailable as exc:
        raise ProviderUnavailable() from exc
    if status == "reversed":
        return True
    if not manual_payout:
        raise InvalidTransition(MESSAGE_REFUND_STILL_PAID)
    return False


def _resolvable_failed(row: payment_selectors.RefundRow) -> bool:
    """The guard of a failed Click refund's resolve (under the Order → Payment locks): the payment's newest
    refund, the payment still ``paid``, no other refund of the payment open or ``succeeded``."""
    payment = payment_selectors.get_payment(row.payment_id)
    if payment is None or payment.status != enums.PaymentStatus.PAID:
        return False
    siblings = [
        r for r in payment_selectors.refunds_for_order(row.order_id) if r.payment_id == row.payment_id
    ]
    if max(r.id for r in siblings) != row.id:
        return False
    return not any(
        r.id != row.id and (r.state in OPEN_REFUND_STATES or r.state == enums.RefundState.SUCCEEDED)
        for r in siblings
    )


def resolve_refund(
    refund_id: int,
    outcome: str,
    actor_id: int | None,
    *,
    manual_payout: bool = False,
    at: datetime | None = None,
    request: Any = None,
) -> payment_services.RefundResult:
    """Close a ``manual_required`` / ``unknown`` refund as ``succeeded`` or ``failed`` (admin §7 item 18
    ``…/resolve/``; with ``actor_id=None`` it stands for Payme's ``CancelTransaction``), or a ``failed``
    **Click** refund as ``succeeded`` (customer contract §2.13.0): a fresh Click status check outside any lock
    (``reversed`` → closed; still ``paid`` → only with ``manual_payout``; Click down → 503; fake Click under
    the production settings → 409), then the guard under the Order → Payment locks (newest refund, payment
    still ``paid``, no other open or succeeded refund; else 409 ``MESSAGE_REFUND_NOT_RESOLVABLE``). The state
    is checked under the Order lock (two concurrent resolves: the second gets 409 ``invalid_transition``);
    writes the ``refund_succeeded`` / ``refund_failed`` event; ``manual_payout`` is audited."""
    _aware(at, "at")
    if outcome not in (enums.RefundState.SUCCEEDED, enums.RefundState.FAILED):
        raise ValueError(f"A refund resolves to succeeded or failed, not {outcome!r}")
    first = payment_selectors.get_refund(refund_id)
    if first is None:
        raise payment_services.RefundNotFound(f"Refund {refund_id} does not exist")
    failed_click = (
        first.state == enums.RefundState.FAILED
        and first.provider == enums.PaymentProvider.CLICK
        and outcome == enums.RefundState.SUCCEEDED
    )
    payout = False
    if failed_click:
        reversed_at_click = _click_status_for_resolve(first, manual_payout)  # no lock held (HTTP)
        payout = manual_payout and not reversed_at_click
    with transaction.atomic():
        order, row = _locked_refund_row(refund_id)
        instant = at or timeutils.now()
        if failed_click:
            if row.state != enums.RefundState.FAILED or not _resolvable_failed(row):
                raise InvalidTransition(MESSAGE_REFUND_NOT_RESOLVABLE)
            result = payment_services.mark_succeeded(
                refund_id,
                instant,
                actor_id=actor_id,
                request=request,
                from_failed=True,
                audit_extra={"manual_payout": payout},
            )
            record_refund_outcome(order, result, at=instant)
            return result
        if row.state not in (enums.RefundState.MANUAL_REQUIRED, enums.RefundState.UNKNOWN):
            raise InvalidTransition(MESSAGE_REFUND_NOT_RESOLVABLE)
        try:
            if outcome == enums.RefundState.SUCCEEDED:
                result = payment_services.mark_succeeded(
                    refund_id, instant, actor_id=actor_id, request=request
                )
            else:
                result = payment_services.mark_failed(
                    refund_id, "resolved_failed", at=instant, actor_id=actor_id, request=request
                )
        except InvalidTransition as exc:  # pragma: no cover - the state was checked under the lock
            raise InvalidTransition(MESSAGE_REFUND_NOT_RESOLVABLE) from exc
        record_refund_outcome(order, result, at=instant)
        return result


def retry_refund(
    refund_id: int, actor_id: int, *, at: datetime | None = None, request: Any = None
) -> payment_services.RefundResult:
    """Retry a ``failed`` refund (admin §7 item 18 ``…/retry/``): a new refund of the payment (Click →
    ``requested`` + outbox, or ``manual_required`` without ``click_paydoc_id``; Payme → ``manual_required``)
    and its ``refund_requested`` event. Refused with the Uzbek messages under the Order lock: the refund is no
    longer ``failed``, its payment is no longer ``paid`` (a later retry already returned the money), another
    refund of the order is open, or — for Click — the Click port is fake under the production settings
    (``MESSAGE_REFUND_CLICK_FAKE``: the retry would skip the status check of a later resolve)."""
    _aware(at, "at")
    with transaction.atomic():
        order, row = _locked_refund_row(refund_id)
        if row.state != enums.RefundState.FAILED:
            raise InvalidTransition(MESSAGE_REFUND_NOT_RETRYABLE)
        if row.provider == enums.PaymentProvider.CLICK and integration_modes.callbacks_refused("click"):
            raise InvalidTransition(MESSAGE_REFUND_CLICK_FAKE)
        payment = payment_selectors.get_payment(row.payment_id)
        if payment is None or payment.status != enums.PaymentStatus.PAID:
            raise InvalidTransition(MESSAGE_REFUND_NOT_RETRYABLE)
        if any(r.state in OPEN_REFUND_STATES for r in payment_selectors.refunds_for_order(row.order_id)):
            raise Conflict(MESSAGE_REFUND_OPEN)
        instant = at or timeutils.now()
        try:
            result = payment_services.retry_refund(
                refund_id,
                actor_id,
                at=instant,
                request=request,
                manual=refund_manual_reason(payment) is not None,
            )
        except InvalidTransition as exc:  # pragma: no cover - the states were checked under the lock
            raise InvalidTransition(MESSAGE_REFUND_NOT_RETRYABLE) from exc
        except Conflict as exc:  # pragma: no cover - idem
            raise Conflict(MESSAGE_REFUND_OPEN) from exc
        record_event(
            order, _E.REFUND_REQUESTED, refund_requested_data(result, payment), actor_id=actor_id, at=instant
        )
        return result


__all__ = [
    "CancelResult",
    "CheckoutError",
    "CommerceError",
    "OrderNotFound",
    "OutOfStock",
    "apply_bts_status",
    "AddressIncomplete",
    "AddressNotFound",
    "CartLineNotFound",
    "CashbackAboveMax",
    "CheckoutInput",
    "InsufficientCashback",
    "PriceChanged",
    "ProductNotFound",
    "add_to_cart",
    "cancel_by_customer",
    "cancel_order",
    "checkout",
    "clear_cart",
    "ensure_payable_payment",
    "late_capture_case",
    "lock_cart",
    "lock_customer",
    "quote_delivery",
    "record_late_capture",
    "refund_manual_reason",
    "refund_requested_data",
    "remove_from_cart",
    "reorder_into_cart",
    "set_cart_quantity",
    "clean_reason",
    "confirm_payment_effects",
    "create_order",
    "delete_cart_lines",
    "expire_due_reservations",
    "expire_reservation",
    "fail_payment_effects",
    "fake_scenario",
    "ledger_instant",
    "lock_order",
    "mark_paid",
    "push_sent",
    "record_event",
    "record_event_once",
    "record_refund_outcome",
    "resolve_refund",
    "retry_refund",
    "retry_shipment",
    "transition",
]


# === M2 customer API (customer contract §2.5–§2.7, §2.13; plan §3 P2)
# ========================================

logger = logging.getLogger(__name__)

# The definitions of customer contract §2.7.2 live in ``selectors`` (read-only predicates); re-exported here.
is_payable = selectors.is_payable
is_open = selectors.is_open
in_flight = selectors.in_flight
LATE_CAPTURE_CASES = ("failed_by_us", "not_payable", "double_payment", "fake_mode")
AUDIT_PAYMENT_LATE_CAPTURE = "payment.late_capture"
FAILED_BY_US = ("expired", "abandoned", "cancelled")


# --- cart (customer contract §2.5, §2.7.4)
# ---------------------------------------------------------------------


def lock_cart(customer_id: int) -> Cart:
    """The customer's ``Cart`` row ``FOR UPDATE`` (created lazily); every cart write takes it first."""
    cart = Cart.objects.select_for_update(of=("self",)).filter(customer_id=customer_id).first()
    if cart is None:
        try:
            with transaction.atomic():
                Cart.objects.create(customer_id=customer_id)
        except IntegrityError:  # a concurrent first write created it
            pass
        cart = Cart.objects.select_for_update(of=("self",)).get(customer_id=customer_id)
    return cart


def _touch(cart: Cart) -> None:
    cart.save()  # TimeStampedModel bumps ``updated_at``


def _sales_on() -> None:
    if not integration_modes.online_sales_enabled():
        raise OnlineSalesUnavailable()


def _product_state(product_id: int) -> tuple[Product, bool, int]:
    product = Product.objects.filter(pk=product_id).first()
    if product is None:
        raise ProductNotFound(f"Product {product_id} does not exist")
    visible = catalog_selectors.customer_visible_products().filter(pk=product_id).exists()
    available = selectors.customer_available_stock([product_id]).get(product_id, 0)
    return product, visible, selectors.max_quantity(available)


def add_to_cart(customer_id: int, product_id: int, quantity: int) -> dict[str, Any]:
    """``POST cart/items/``: add ``quantity`` to the line, clamped to ``max_quantity`` (``max_reached``);
    returns ``last_change``. Refusals: online selling off (first), unknown product, not visible
    (``product_unavailable``), ``max_quantity = 0`` (``OUT_OF_STOCK`` one), a new line in a full cart."""
    _sales_on()
    with transaction.atomic():
        cart = lock_cart(customer_id)
        product, visible, limit = _product_state(product_id)
        if not visible:
            raise ProductUnavailable()
        if limit == 0:
            raise OutOfStockError(one=True, details={"product_id": product_id, "available": 0})
        line = CartItem.objects.filter(cart=cart, product_id=product_id).first()
        if line is None and CartItem.objects.filter(cart=cart).count() >= settings.CART_MAX_LINES:
            raise CartFull(
                details={"max_lines": settings.CART_MAX_LINES}, params={"limit": settings.CART_MAX_LINES}
            )
        requested = (line.quantity if line is not None else 0) + quantity
        new = min(requested, limit)
        now = timeutils.now()
        if line is None:
            CartItem.objects.create(
                cart=cart,
                product=product,
                quantity=new,
                price_at_add=product.price,
                added_at=now,
                updated_at=now,
            )
        else:
            line.quantity = new
            line.price_at_add = product.price
            line.updated_at = now
            line.save(update_fields=["quantity", "price_at_add", "updated_at"])
        _touch(cart)
        return {
            "product_id": product_id,
            "requested_quantity": requested,
            "quantity": new,
            "max_reached": new < requested,
        }


def set_cart_quantity(customer_id: int, product_id: int, quantity: int) -> dict[str, Any]:
    """``PATCH cart/items/{product_id}/``: absolute quantity, clamped to ``max_quantity``; returns
    ``last_change``. Online selling off (first) → 409; line absent → :class:`CartLineNotFound`; not visible →
    ``product_unavailable``; ``max_quantity = 0`` → ``OUT_OF_STOCK`` (line unchanged)."""
    _sales_on()
    with transaction.atomic():
        cart = lock_cart(customer_id)
        line = CartItem.objects.filter(cart=cart, product_id=product_id).select_related("product").first()
        if line is None:
            raise CartLineNotFound(f"No cart line for product {product_id}")
        product, visible, limit = _product_state(product_id)
        if not visible:
            raise ProductUnavailable()
        if limit == 0:
            raise OutOfStockError(one=True, details={"product_id": product_id, "available": 0})
        new = min(quantity, limit)
        line.quantity = new
        line.price_at_add = product.price
        line.updated_at = timeutils.now()
        line.save(update_fields=["quantity", "price_at_add", "updated_at"])
        _touch(cart)
        return {
            "product_id": product_id,
            "requested_quantity": quantity,
            "quantity": new,
            "max_reached": new < quantity,
        }


def remove_from_cart(customer_id: int, product_ids: Sequence[int]) -> int:
    """``DELETE cart/items/{product_id}/`` and the bulk form: remove those lines (absent ids ignored)."""
    with transaction.atomic():
        cart = lock_cart(customer_id)
        deleted, _ = CartItem.objects.filter(cart=cart, product_id__in=list(product_ids)).delete()
        _touch(cart)
        return deleted


def clear_cart(customer_id: int) -> int:
    """``DELETE cart/``: empty the cart (``delete_cart_lines`` under the Cart lock)."""
    with transaction.atomic():
        cart = lock_cart(customer_id)
        deleted = delete_cart_lines(customer_id)
        _touch(cart)
        return deleted


def reorder_into_cart(order_id: int, customer_id: int) -> None:
    """``POST orders/{id}/reorder/``: the order's lines back into the cart (the caller checked ownership and
    the history horizon). An existing line keeps the larger of both quantities, clamped to ``max_quantity``;
    unavailable or out-of-stock products are added anyway (shown with their status), except when the cart
    would exceed ``CART_MAX_LINES``: they are skipped first, and if it is still too full → ``cart_full``,
    nothing written."""
    with transaction.atomic():
        cart = lock_cart(customer_id)
        lines = list(
            OrderItem.objects.filter(order_id=order_id).select_related("product").order_by("position")
        )
        existing = {item.product_id: item for item in CartItem.objects.filter(cart=cart)}
        ids = [line.product_id for line in lines]
        available = selectors.customer_available_stock(ids)
        visible = selectors.visible_ids(ids)
        plan: list[tuple[OrderItem, int, bool]] = []
        for line in lines:
            limit = selectors.max_quantity(available.get(line.product_id, 0))
            sellable = line.product_id in visible and limit > 0
            current = existing[line.product_id].quantity if line.product_id in existing else 0
            wanted = max(current, line.quantity)
            if sellable:
                quantity = min(wanted, limit)
            else:
                quantity = min(max(wanted, 1), int(settings.CART_MAX_QTY_PER_LINE), 99)
            plan.append((line, quantity, sellable))
        new_lines = [entry for entry in plan if entry[0].product_id not in existing]
        if len(existing) + len(new_lines) > settings.CART_MAX_LINES:
            plan = [entry for entry in plan if entry[2] or entry[0].product_id in existing]
            if (
                len(existing) + sum(1 for e in plan if e[0].product_id not in existing)
                > settings.CART_MAX_LINES
            ):
                raise CartFull(
                    details={"max_lines": settings.CART_MAX_LINES}, params={"limit": settings.CART_MAX_LINES}
                )
        now = timeutils.now()
        for line, quantity, _sellable in plan:
            item = existing.get(line.product_id)
            if item is None:
                CartItem.objects.create(
                    cart=cart,
                    product_id=line.product_id,
                    quantity=quantity,
                    price_at_add=line.product.price,
                    added_at=now,
                    updated_at=now,
                )
            else:
                item.quantity = quantity
                item.price_at_add = line.product.price
                item.updated_at = now
                item.save(update_fields=["quantity", "price_at_add", "updated_at"])
        _touch(cart)


def delete_cart_lines(customer_id: int) -> int:
    """Delete every cart line of the customer (``DELETE me/`` step 4, the dormant reset of customer contract
    §2.2.3, :func:`clear_cart`); returns the number of lines deleted."""
    deleted, _ = CartItem.objects.filter(cart__customer_id=customer_id).delete()
    return deleted


# --- checkout (customer contract §2.7.1)
# -------------------------------------------------------------------------


def quote_delivery(region_code: str, city_code: str, scenario: Mapping[str, Any] | None = None) -> int:
    """The BTS tariff (no row lock may be held): a route BTS does not serve → 409 ``delivery_unavailable``
    (``error.delivery_unavailable``); a transport failure → 503 ``provider_unavailable``."""
    try:
        return get_bts(scenario).quote(region_code, city_code)
    except BtsRejected as exc:
        raise DeliveryUnavailable(key="error.delivery_unavailable") from exc
    except BtsUnavailable as exc:
        raise ProviderUnavailable() from exc


@dataclass(frozen=True, slots=True)
class CheckoutInput:
    fulfilment: str
    payment_provider: str
    channel: str
    expected_total: int
    cashback_to_use: int = 0
    address_id: int | None = None
    region_code: str = ""
    city_code: str = ""
    customer_note: str = ""
    scenario: Mapping[str, Any] | None = None  # X-GD-Fake-Scenario (local/test only), copied to metadata.fake
    checkout_meta: Mapping[str, Any] | None = None  # metadata.checkout: app_version, device_id


def lock_customer(customer_id: int) -> Customer | None:
    """The customer-level lock of the customer API: ``SELECT … FOR NO KEY UPDATE`` (never ``FOR UPDATE``,
    which would deadlock with a capture's ``Purchase`` insert; customer contract §2.7.1)."""
    return Customer.objects.select_for_update(no_key=True, of=("self",)).filter(pk=customer_id).first()


def checkout(customer_id: int, data: CheckoutInput) -> int:
    """``POST orders/checkout/`` after the view's body, consent and address checks (rows 1–3): row 0 (online
    selling), row 4 (the BTS quote, no row lock held), then the customer-level lock (``FOR NO KEY UPDATE``; an
    inactive customer → 401 ``SESSION_EXPIRED``), row 5 (open orders), row 6 (cart read under the lock and the
    Cart lock) and :func:`create_order` (rows 7–10); the cart is emptied and ``Cart.last_checkout_order`` set
    in the same transaction. Returns the order id."""
    _sales_on()
    delivery = data.fulfilment == enums.FulfilmentMethod.DELIVERY
    scenario = dict(data.scenario or {})
    bts_scenario = scenario.get("bts") if isinstance(scenario.get("bts"), dict) else None
    fee = quote_delivery(data.region_code, data.city_code, bts_scenario) if delivery else 0
    with transaction.atomic():
        customer = lock_customer(customer_id)
        if customer is None or customer.status != enums.CustomerStatus.ACTIVE:
            raise AuthFailed("SESSION_EXPIRED")
        open_orders = Order.objects.filter(customer_id=customer_id, status=_S.AWAITING_PAYMENT).order_by(
            "-created_at", "-id"
        )
        if open_orders.count() >= settings.CHECKOUT_MAX_OPEN_ORDERS:
            newest = open_orders.first()
            assert newest is not None  # noqa: S101 - count ≥ 1
            raise OpenOrderExists(
                details={
                    "order_id": newest.pk,
                    "pay_by": (
                        timeutils.isoformat(newest.reservation_expires_at)
                        if newest.reservation_expires_at is not None
                        else None
                    ),
                },
                params={"order_id": newest.pk},
            )
        cart = lock_cart(customer_id)
        items = list(
            CartItem.objects.filter(cart=cart)
            .order_by("added_at", "id")
            .values_list("product_id", "quantity")
        )
        if not items:
            raise CartEmpty()
        metadata: dict[str, Any] = {"checkout": dict(data.checkout_meta or {})}
        if scenario:
            metadata["fake"] = scenario
        order_id = create_order(
            customer_id,
            [(int(product_id), int(quantity)) for product_id, quantity in items],
            data.fulfilment,
            payment_provider=data.payment_provider,
            channel=data.channel,
            delivery_fee=fee,
            address_id=data.address_id if delivery else None,
            cashback_used=data.cashback_to_use,
            expected_total=data.expected_total,
            min_payable=int(settings.CHECKOUT_MIN_PAYABLE),
            visible_only=True,
            customer_note=data.customer_note,
            metadata=metadata,
        )
        CartItem.objects.filter(cart=cart).delete()
        cart.last_checkout_order_id = order_id
        cart.save()
        return order_id


# --- pay (customer contract §2.7.2)
# --------------------------------------------------------------------------------


def ensure_payable_payment(
    order_id: int, provider: str, *, superseding: str | None = None, at: datetime | None = None
) -> payment_selectors.PaymentRow:
    """The payment a provider page / callback works on, under ``lock_order`` → Payment locks.

    Same provider → the active ``pending`` payment is reused as it is (nothing written); with
    ``superseding=<click_trans_id>`` (Click Prepare only) a Click payment attached to **another** transaction
    is failed ``abandoned`` (no event) and a new ``pending`` row is created; a Payme row is reused only while
    it has no transaction (``attach_transaction`` refuses another id with ``TransactionTaken``). Other
    provider: in flight → :class:`PaymentInProgress`; else the pending row is failed ``abandoned`` and a new
    one created (``Order.payment_provider`` updated). A failed previous attempt gets a new ``pending`` row. An
    order with a non-late ``paid`` payment raises ``AlreadyPaid``."""
    with transaction.atomic():
        order = lock_order(order_id)
        now = at or timeutils.now()
        if selectors.honoured_paid(order_id):
            raise AlreadyPaid()
        pending = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
        if pending is not None and pending.provider == provider:
            superseded = (
                superseding is not None
                and provider == enums.PaymentProvider.CLICK
                and bool(pending.transaction_id)
                and pending.transaction_id != superseding
            )
            if not superseded:
                return pending
            payment_services.fail_payment(pending.id, "abandoned", now, unilateral=True)
        elif pending is not None:
            if selectors.in_flight(pending, now):
                raise PaymentInProgress(
                    details={"provider": pending.provider, "order_id": order_id},
                    params={"provider": selectors.provider_brand(pending.provider)},
                )
            result = payment_services.fail_payment(pending.id, "abandoned", now, unilateral=True)
            if not result.changed:  # a Payme state-1 transaction is never failed unilaterally (P-2)
                raise PaymentInProgress(
                    details={"provider": pending.provider, "order_id": order_id},
                    params={"provider": selectors.provider_brand(pending.provider)},
                )
        created = payment_services.create_payment(order_id, provider, order.total)
        if order.payment_provider != provider:
            order.payment_provider = provider
            order.save(update_fields=["payment_provider", "updated_at"])
        row = payment_selectors.get_payment(created.payment_id)
        assert row is not None  # noqa: S101 - just created
        return row


# --- customer cancel (customer contract §2.7.3; state.py row 5) ---------------------------------------------


def cancel_by_customer(order_id: int, customer_id: int, *, at: datetime | None = None) -> int:
    """Cancel an ``awaiting_payment`` order of the customer (C-6); returns the order id.

    Another customer's order → :class:`OrderNotFound`; any other status → 409 ``not_cancellable``
    (``error.order_not_cancellable``); a **Click** payment in flight → 409 ``payment_in_progress``. The
    pending payment fails unilaterally (``cancelled``; a Payme state-1 transaction stays ``pending`` until
    Payme's ``CancelTransaction``), reservations and the hold are released, ``cancelled`` /
    ``customer_request`` with ``cancelled_by`` null, event ``cancelled`` (source system), push ``cancelled``.
    No audit."""
    _aware(at, "at")
    with transaction.atomic():
        order = lock_order(order_id)
        if order.customer_id != customer_id:
            raise OrderNotFound(f"Order {order_id} does not exist")
        if order.status != _S.AWAITING_PAYMENT:
            raise not_cancellable("order")
        instant = at or timeutils.now()
        pending = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PENDING)
        if (
            pending is not None
            and pending.provider == enums.PaymentProvider.CLICK
            and in_flight(pending, instant)
        ):
            raise PaymentInProgress(
                details={"provider": pending.provider, "order_id": order.pk}, params={"provider": "Click"}
            )
        if pending is not None:
            payment_services.fail_payment(pending.id, "cancelled", instant, unilateral=True)
        stock_released = (
            StockReservation.objects.filter(order=order, state=StockReservation.State.ACTIVE).update(
                state=StockReservation.State.RELEASED,
                released_at=instant,
                release_reason=StockReservation.ReleaseReason.CANCELLED,
                updated_at=instant,
            )
            > 0
        )
        loyalty_services.release_hold(order.pk, "order_cancelled", at=instant)
        _set_status(
            order, _S.CANCELLED, instant, cancellation_reason=enums.CancellationReason.CUSTOMER_REQUEST
        )
        cancelled = record_event(
            order,
            _E.CANCELLED,
            {
                "reason": enums.CancellationReason.CUSTOMER_REQUEST,
                "note": None,
                "refund_id": None,
                "cashback_reversed": False,
                "stock_released": stock_released,
                "bts_cancelled": False,
                "push_sent": push_sent(order.customer_id),
            },
            source=_SRC.SYSTEM,
            at=instant,
        )
        _enqueue_push(order, _S.CANCELLED, cancelled)
        return order.pk


# --- refused captures (customer contract §2.13.0)
# ----------------------------------------------------------------


def late_capture_case(order: Order, payment: payment_selectors.PaymentRow, now: datetime) -> str | None:
    """The refused-capture case of a capture the provider reports for ``payment`` (``None`` = honour it),
    customer contract §2.13.0: ``double_payment`` (the order is paid by another payment), ``failed_by_us``
    (failed by us for a non-provider reason, incl. a superseded attachment), ``not_payable`` (cancelled, or
    past the grace); a payment that is not the order's pending one is never honoured."""
    if selectors.paid_by_other(order.pk, payment.id) or order.status in state.PAID_STATUSES:
        return "double_payment"
    if payment.status == enums.PaymentStatus.FAILED and payment.error_code in FAILED_BY_US:
        return "failed_by_us"
    if not is_payable(order, payment, now, attaching=False):
        return "not_payable"
    if payment.status != enums.PaymentStatus.PENDING:
        return "failed_by_us"
    return None


def record_late_capture(
    order_id: int,
    payment_id: int,
    provider: str,
    provider_txn_id: str,
    *,
    case: str,
    at: datetime | None = None,
    raw: dict[str, Any] | None = None,
) -> payment_services.RefundResult | None:
    """Accept and reverse a capture we do not honour (customer contract §2.13.0) under Order → Payment:
    ``payments.mark_late_captured`` (``paid`` + ``late_capture``), ``Refund(origin=late_capture)`` (Click
    ``requested`` + outbox ``refund.submit``, or ``manual_required`` without ``click_paydoc_id`` / with fake
    Click), event ``refund_requested`` (source system, with ``manual_reason``), audit ``payment.late_capture
    {order_id, payment_id, provider, case}`` and ERROR ``payment.late_capture``. The order never changes
    status. ``None`` when the payment was already recorded as a late capture (a replay)."""
    if case not in LATE_CAPTURE_CASES:
        raise ValueError(f"Unknown late-capture case {case!r}")
    _aware(at, "at")
    with transaction.atomic():
        order = lock_order(order_id)
        instant = at or timeutils.now()
        payment = payment_selectors.get_payment(payment_id)
        if payment is None or payment.order_id != order_id or payment.provider != provider:
            raise InvalidTransition(f"Payment {payment_id} is not a {provider} payment of order {order_id}")
        captured = payment_services.mark_late_captured(payment_id, provider_txn_id, instant, raw)
        if not captured.changed:
            return None
        row = payment_selectors.get_payment(payment_id)
        assert row is not None  # noqa: S101
        refund = payment_services.create_refund(
            payment_id,
            row.amount,
            None,
            Refund.Origin.LATE_CAPTURE,
            at=instant,
            manual=refund_manual_reason(row) is not None,
        )
        record_event_once(order, _E.REFUND_REQUESTED, refund_requested_data(refund, row), at=instant)
        details = {"order_id": order_id, "payment_id": payment_id, "provider": provider, "case": case}
        audit.record(
            action=AUDIT_PAYMENT_LATE_CAPTURE,
            entity_type="payment",
            entity_id=str(payment_id),
            actor_type="system",
            after=details,
        )
        logger.error("payment.late_capture %s", details)
        return refund
