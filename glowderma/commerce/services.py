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
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from glowderma.accounts.models import Address, Customer, Device, StaffUser
from glowderma.catalog import billz as catalog_billz
from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import events, locks, quoting, selectors, state, topics
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
from glowderma.core import alerts, audit, enums, integration_modes, outbox, timeutils
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
from glowderma.core.uzsource import N_
from glowderma.delivery import pricing as delivery_pricing
from glowderma.delivery import quotes as delivery_quotes
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery import yandex as delivery_yandex
from glowderma.delivery.models import Shipment
from glowderma.integrations.billz.registry import billz_mode
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click.ports import ClickUnavailable
from glowderma.integrations.click.registry import get_click
from glowderma.integrations.yandex_delivery.ports import (
    CANCEL_FREE,
    CANCEL_PAID,
    CANCEL_UNAVAILABLE,
    CancelTerms,
    Claim,
    YandexDeliveryPort,
    YandexError,
    YandexUnavailable,
)
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services
from glowderma.payments.models import OPEN_REFUND_STATES, Refund
from glowderma.site_config.models import SiteSettings

MESSAGE_NOT_CANCELLABLE = N_("Bu buyurtmani bekor qilib bo‘lmaydi.")
MESSAGE_BTS_REFUSED = N_("BTS buyurtmani bekor qilmadi: {reason}.")
MESSAGE_INVALID_TRANSITION = N_("Bu holatga o‘tib bo‘lmaydi.")
MESSAGE_RETRY_REFUSED = N_("BTS ga qayta yuborib bo‘lmaydi.")
MESSAGE_RETRY_PENDING = N_("BTS so‘rovi allaqachon yuborilmoqda.")
MESSAGE_REFUND_NOT_RESOLVABLE = N_("Bu qaytarishni yakunlab bo‘lmaydi.")
MESSAGE_REFUND_NOT_RETRYABLE = N_("Bu qaytarishni qayta yuborib bo‘lmaydi.")
MESSAGE_REFUND_OPEN = N_("Bu to‘lov uchun qaytarish allaqachon jarayonda.")
MESSAGE_ORDER_BUSY = N_("Buyurtma boshqa amal bilan band. Birozdan so‘ng qayta urinib ko‘ring.")
# The 409 code of "the order row is held by another change" (admin contract §1.6): transient, retry later.
CODE_ORDER_BUSY = "order_busy"
MESSAGE_PURCHASE_CONFLICT = N_("Buyurtma xaridi boshqa mijozga tegishli. Ma’lumotlarni tekshiring.")
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
    the row is locked): the admin paths pass :data:`ADMIN_LOCK_WAIT` and answer 409 ``order_busy``
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
                raise Conflict(MESSAGE_ORDER_BUSY, code=CODE_ORDER_BUSY) from exc
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
    delivery_provider: str = enums.DeliveryProvider.BTS,
    delivery_method: str = "",
    delivery_quote_id: UUID | None = None,
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

    ``delivery_provider="yandex"`` (checkout with a locked quote, plan §3.4) needs a saved address with a pin
    instead of the BTS region and city: the pin is snapshotted into ``address_latitude/longitude`` and the
    shipment is created for Yandex with ``delivery_method`` and the quote.
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
    yandex = delivery and delivery_provider == enums.DeliveryProvider.YANDEX
    if delivery_provider not in enums.DeliveryProvider.values or (yandex and not delivery_method):
        raise CheckoutError("A Yandex shipment needs its method")
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
        pin: dict[str, Any] = {}
        if delivery:
            if yandex and address_id is None:
                raise AddressNotFound("A Yandex delivery needs a saved address")
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
                if yandex:
                    if saved.latitude is None or saved.longitude is None:
                        raise AddressIncomplete("A Yandex delivery address needs its map pin")
                    pin = {"address_latitude": saved.latitude, "address_longitude": saved.longitude}
            else:
                address = {
                    "text": address_text,
                    "region_code": region_code,
                    "city_code": city_code,
                    "comment": address_comment,
                }
            if not address["text"]:
                raise AddressNotFound("A delivery order needs an address")
            if not yandex and (not address["region_code"] or not address["city_code"]):
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
            **pin,
            recipient_name=customer.name,
            recipient_phone=customer.phone or "",  # checkout is phone-gated (C§2.2.10)
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
        if yandex:
            delivery_services.ensure_shipment(
                order.pk, provider=delivery_provider, method=delivery_method, quote_id=delivery_quote_id
            )
        elif delivery:
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
    (the Billz order UUID is persisted on the purchase first), ``bts.create_shipment`` for a BTS delivery
    (a Yandex shipment only becomes ``awaiting_dispatch``: nothing goes to Yandex at payment, plan §3.5) and
    ``push.order_status processing``. No audit (system).

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
            if delivery_selectors.shipment_provider(order.pk) == enums.DeliveryProvider.YANDEX:
                delivery_services.mark_awaiting_dispatch(order.pk, at=instant)
            else:
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


# --- Billz settlement of sold units (03 §1.6, review NEW-2) -------------------------------------------------


def settle_billz_reservations(
    product_ids: Collection[int] | None = None, *, at: datetime | None = None
) -> int:
    """Mark the converted reservations Billz's stock already reflects as settled; returns how many.

    ``selectors.available_stock`` subtracts a converted reservation until its sale is in Billz (the purchase's
    ``billz_pushed_at``: the live push, or staff recording it by hand) **and** the stock was read after that
    (``billz_pushed_at < ProductStock.synced_at``, the reservation converted before that read), or — for a
    cancelled order whose sale never reached Billz and whose payment was never attempted there — once the
    stock was read after the conversion. Once true, that never becomes false again, so the reservation is
    stamped ``billz_settled_at`` and the availability read (``reservation_unsettled_prod_idx``) never looks at
    it again. Run after every Billz import (outbox ``catalog.stock_synced``), after a push succeeded and after
    a sale was recorded by hand; idempotent. ``product_ids`` limits it to those products.
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    synced = F("product__stock__synced_at")
    eligible = StockReservation.objects.filter(
        Q(order__purchase__billz_pushed_at__lt=synced)
        | Q(
            order__purchase__billz_pushed_at__isnull=True,
            order__purchase__billz_payment_attempted_at__isnull=True,
            order__status=_S.CANCELLED,
        ),
        state=StockReservation.State.CONVERTED,
        billz_settled_at__isnull=True,
        product__stock__synced_at__isnull=False,
        converted_at__lte=synced,
    )
    if product_ids is not None:
        eligible = eligible.filter(product_id__in=list(product_ids))
    return StockReservation.objects.filter(
        pk__in=eligible.values("pk"), billz_settled_at__isnull=True
    ).update(billz_settled_at=instant, updated_at=instant)


def settle_order_reservations(order_id: int, *, at: datetime | None = None) -> int:
    """Settle every converted reservation of one order at once: its sale will never reach a Billz this
    system reads stock from (Billz is fake under the production settings: staff enter the sale by hand,
    customer contract §6.14), so the units must never start counting at the first live read."""
    _aware(at, "at")
    instant = at or timeutils.now()
    return StockReservation.objects.filter(
        order_id=order_id, state=StockReservation.State.CONVERTED, billz_settled_at__isnull=True
    ).update(billz_settled_at=instant, updated_at=instant)


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

    def announce() -> None:
        logger.error(
            "billz.refund_by_hand order_id=%s billz_order_id=%s pushed=%s: the order was cancelled; "
            "refund the Billz sale (or delete its draft) in Billz by hand",
            order_id,
            sale_id,
            pushed,
        )
        alerts.alert(
            "billz.refund_by_hand",
            {"order_id": order_id, "purchase_id": purchase_id, "pushed": pushed},
            scope=str(order_id),  # one alert per order: each is a refund staff make by hand
        )

    transaction.on_commit(announce)


def cancel_order(
    order_id: int, reason: str, actor_id: int, *, at: datetime | None = None, request: Any = None
) -> CancelResult:
    """``POST orders/{id}/cancel/`` (contract §2.2 table; 03 §4.1 rows 4/6/7).

    * ``awaiting_payment`` → the pending payment fails (Payme state 1 excepted), reservations and the hold
      are released;
    * a Yandex shipment never calls BTS or Yandex here: the order is cancelled only when it is carrier-safe
      (:func:`yandex_cancel_refusal`: no open claim — :func:`cancel_at_carrier` ends one before pickup — and
      the parcel not with a courier, or the parcel returning / back; plan §3.8), else 409 with the Uzbek
      reason; a claim Yandex cancelled at our request makes ``bts_cancelled`` true and the shipment
      ``cancelled``; the order's dispatch / preparation / failed-delivery / cancel_incomplete issues close;
    * ``processing`` / ``handed_to_courier`` → a BTS parcel that exists is cancelled **first**, synchronously
      (refusal → 409 ``not_cancellable`` with BTS's reason; transport failure → 503 ``provider_unavailable``;
      nothing is written before the answer, and every check that can refuse runs before the call; BTS
      answers ``ok`` again for a parcel it already cancelled, so a cancel rolled back after BTS agreed is
      simply repeated); then a Refund (Click ``requested`` + outbox, Payme
      ``manual_required``; the payment stays ``paid``) and one reversal per unreversed debit/credit;
    * ``delivered`` / ``cancelled`` → 409 ``not_cancellable``.

    Effects: ``cancelled`` (reason ``admin``, note, ``cancelled_by``), events ``cancelled`` (+
    ``refund_requested``), push ``cancelled``, audit ``order.cancelled``. The Order lock is awaited at most
    :data:`ADMIN_LOCK_WAIT` (409 ``order_busy``); the reversal instant is :func:`ledger_instant`.
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
        yandex = False
        paid = None
        purchase = None
        if order.status in (_S.PROCESSING, _S.HANDED_TO_COURIER):
            # everything that can refuse runs before the BTS cancel, the only effect outside the database
            paid = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PAID)
            purchase = loyalty_selectors.purchase_for_order(order.pk)
            shipment = delivery_selectors.shipment_info(order.pk)
            yandex = shipment is not None and shipment.provider == enums.DeliveryProvider.YANDEX
            has_parcel = shipment is not None and shipment.is_bts and shipment.state == Shipment.State.CREATED
            if yandex:
                # carrier-safe only (plan §3.8): cancel_at_carrier has ended every open claim before pickup;
                # a parcel the courier took is refundable only once Yandex brings it back
                refusal = yandex_cancel_refusal(delivery_selectors.yandex_cancel_view(order.pk))
                if refusal is not None:
                    raise NotCancellable(refusal)
                bts_cancelled = delivery_services.mark_yandex_cancelled(order.pk, at=instant)
            elif order.status == _S.HANDED_TO_COURIER and not has_parcel:
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
                        MESSAGE_BTS_REFUSED.format(reason=outcome.reason or N_("posilka kuryerda"))
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
                    manual_reason=refund_manual_reason(paid) or "",
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
        if yandex:  # what a cancelled order no longer needs (a returning parcel stays return_pending)
            for kind in CANCEL_RESOLVES:
                delivery_services.resolve_kind(order.pk, kind, RESOLVED_BY_CANCEL_NOTE)
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
    """``POST orders/{id}/transition/``: one of ``available_actions.transitions`` (state.py rows 8–10; none
    for a Yandex order but the ``parcel_unaccounted`` exception), else 409 ``invalid_transition``. Event
    ``status_changed``, push, audit ``order.status_changed``."""
    _aware(at, "at")
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        provider = delivery_selectors.shipment_provider(order.pk) or enums.DeliveryProvider.BTS
        unaccounted = provider == enums.DeliveryProvider.YANDEX and delivery_selectors.has_open_issue(
            order.pk, enums.DeliveryIssueKind.PARCEL_UNACCOUNTED
        )
        offered = state.manual_transitions(
            order.status, order.fulfilment, provider=provider, parcel_unaccounted=unaccounted
        )
        if status not in offered:
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
        if unaccounted and status == _S.DELIVERED:  # staff confirmed the hand-over Yandex lost track of
            delivery_services.mark_delivered_by_staff(order.pk, actor_id=actor_id, at=instant)
            for kind in (enums.DeliveryIssueKind.PARCEL_UNACCOUNTED, *DELIVERED_RESOLVES):
                delivery_services.resolve_kind(order.pk, kind, RESOLVED_BY_STAFF_DELIVERED_NOTE)
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
    """``POST orders/{id}/retry-shipment/``: delivery ∧ ``processing`` ∧ BTS shipment ``failed`` (else 409
    ``invalid_transition``; a Yandex shipment never), not ``pending`` (else 409 ``conflict``); enqueues a
    manual ``bts.create_shipment`` and audits ``order.shipment_retry_requested``. Both checks run under the
    Order lock, so two concurrent retries cannot both enqueue."""
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        shipment = delivery_selectors.shipment_info(order.pk)
        open_delivery = order.fulfilment == enums.FulfilmentMethod.DELIVERY and order.status == _S.PROCESSING
        if (
            shipment is None
            or not shipment.is_bts
            or not open_delivery
            or shipment.state != Shipment.State.FAILED
        ):
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


# --- Yandex Delivery: dispatch (plan §3.6) and the claim writer (§3.7) -------------------------------------

AUDIT_DISPATCH_REQUESTED = "order.dispatch_requested"
MESSAGE_DISPATCH_REFUSED = N_("Bu buyurtma uchun kuryer chaqirib bo‘lmaydi.")
MESSAGE_DISPATCH_ACTIVE = N_("Kuryer allaqachon chaqirilgan.")
MESSAGE_DISPATCH_CANCELLING = N_("Buyurtma bekor qilinmoqda.")
MESSAGE_DISPATCH_NO_PIN = N_("Manzilning xaritadagi nuqtasi yo‘q.")
MESSAGE_DISPATCH_NO_REVIEW = N_("Bu buyurtmada tasdiqlanadigan narx yo‘q.")
MESSAGE_DISPATCH_METHOD_DISABLED = N_("Bu yetkazish usuli o‘chirilgan.")
MESSAGE_DISPATCH_TOO_HEAVY = N_("Jo‘natma bu yetkazish usuli uchun juda og‘ir.")
# The warehouse calendar's refusals (``delivery.warehouse.DispatchRefusal``), answered with their own code.
MESSAGE_DISPATCH_REFUSALS: dict[str, str] = {
    "not_configured": N_("Ombor ma’lumotlari to‘liq kiritilmagan."),
    "suspended": N_("Kuryer chaqirish vaqtincha to‘xtatilgan."),
    "closed": N_("Ombor hozir yopiq."),
    "after_cutoff": N_("Bugun kuryer chaqirish vaqti tugadi."),
    "returns_unavailable": N_("Ombor hozir qaytgan jo‘natmalarni qabul qilmaydi."),
}
# A delivered order closes the cases a failed hand-over or an uncertain dispatch had opened.
DELIVERED_RESOLVES: tuple[str, ...] = (
    enums.DeliveryIssueKind.DELIVERY_FAILED,
    enums.DeliveryIssueKind.RETURN_PENDING,
    enums.DeliveryIssueKind.DISPATCH_UNKNOWN,
)
DISPATCH_RESOLVES: tuple[str, ...] = (
    enums.DeliveryIssueKind.DISPATCH_FAILED,
    enums.DeliveryIssueKind.DISPATCH_REVIEW,
    enums.DeliveryIssueKind.PREPARATION_OVERDUE,  # the parcel is prepared and a courier called
)
RESOLVED_BY_DISPATCH_NOTE = "Kuryer qayta chaqirildi."
RESOLVED_BY_STATUS_NOTE = "Yandex holati bilan hal bo‘ldi."
# The ``source`` of a claim snapshot (commerce.yandex_flow) and the shipment ``via`` it is recorded with.
CLAIM_SOURCES: dict[str, str] = {
    "callback": enums.ShipmentVia.WEBHOOK,
    "poll": enums.ShipmentVia.POLLING,
    "dispatch": enums.ShipmentVia.POLLING,
    "cancel": enums.ShipmentVia.POLLING,
    "dev": enums.ShipmentVia.POLLING,
}


def parked_stage(order: Order, stage: str) -> str:
    """The stage a dispatch that summoned no courier leaves the shipment at: ``stage`` (``failed`` /
    ``awaiting_dispatch``), except for an order whose parcel came back earlier (``handed_to_courier``: a
    re-dispatch after a return) — it is still at the warehouse, ``returned``, and may be dispatched again."""
    if order.status == _S.HANDED_TO_COURIER:
        return enums.ShipmentStage.RETURNED
    return stage


def _dispatch_error(code: str) -> Exception:
    if code == selectors.DISPATCH_CLAIM_OPEN:
        return Conflict(MESSAGE_DISPATCH_ACTIVE)
    if code == selectors.DISPATCH_CANCEL_REQUESTED:
        return Conflict(MESSAGE_DISPATCH_CANCELLING)
    if code == selectors.DISPATCH_NO_PIN:
        return Conflict(MESSAGE_DISPATCH_NO_PIN, code=code)
    if code in MESSAGE_DISPATCH_REFUSALS:
        return Conflict(MESSAGE_DISPATCH_REFUSALS[code], code=code)
    return InvalidTransition(MESSAGE_DISPATCH_REFUSED)


def open_delivery_issue(
    order_id: int, kind: str, code: str, *, urgent: bool = False, details: Mapping[str, Any] | None = None
) -> int:
    """Open (or refresh) the order's ``kind`` issue (``delivery.services.open_issue``, which alerts
    ``delivery.issue_opened`` once the issue becomes urgent — in this transaction, so only after it
    commits). ``details`` holds ids and amounts only."""
    body = {"order_id": order_id, **dict(details or {})}
    issue_id, _created = delivery_services.open_issue(order_id, kind, code=code, urgent=urgent, details=body)
    logger.info("yandex.issue %s", {"order_id": order_id, "kind": str(kind), "code": code, "urgent": urgent})
    return issue_id


def request_dispatch(
    order_id: int,
    actor_id: int,
    *,
    weight_g: int,
    method: str | None = None,
    approved_price: int | None = None,
    request: Any = None,
) -> int:
    """``POST orders/{id}/dispatch/`` (plan §3.6): staff checked the contents and weighed the parcel.

    Under the Order lock (awaited at most :data:`ADMIN_LOCK_WAIT`, 409 ``order_busy``) every refusal of
    :func:`selectors.dispatch_blocker` is answered (409 ``invalid_transition`` / ``conflict`` /
    ``coordinates_required`` / the warehouse calendar's code); a switched ``method`` must be enabled (409
    ``method_disabled``) and its class carry the weight (409 ``too_heavy``); ``approved_price`` needs an open
    ``dispatch_review`` issue (409 ``conflict``). Then the parcel and a new open ``CourierClaim`` are stored
    (``delivery.services.start_dispatch``: a double click loses on the open-claim index → 409 ``conflict``),
    ``yandex.dispatch`` is enqueued (dedupe: the claim's ``request_id``; nothing goes to Yandex in the
    request), the order's open ``dispatch_failed`` / ``dispatch_review`` issues are resolved and the step is
    audited ``order.dispatch_requested``. The paid fee never changes. Returns the claim pk.
    """
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        now = timeutils.now()
        shipment = delivery_selectors.yandex_shipment(order.pk)
        blocked = selectors.dispatch_blocker(order, shipment, now=now)
        if blocked is not None or shipment is None:
            raise _dispatch_error(blocked or selectors.DISPATCH_NOT_ALLOWED)
        info = delivery_selectors.shipment_info(order.pk)
        if info is None or info.claim_open:
            raise Conflict(MESSAGE_DISPATCH_ACTIVE)
        chosen = method or shipment.method
        config = delivery_selectors.method_settings(chosen)
        if chosen != shipment.method and not config.enabled:
            raise Conflict(MESSAGE_DISPATCH_METHOD_DISABLED, code="method_disabled")
        limit = delivery_quotes.CLASS_WEIGHT_LIMIT_G.get(config.taxi_class)
        if limit is not None and weight_g > limit:
            raise Conflict(MESSAGE_DISPATCH_TOO_HEAVY, code="too_heavy")
        if approved_price is not None and not delivery_selectors.has_open_issue(
            order.pk, enums.DeliveryIssueKind.DISPATCH_REVIEW
        ):
            raise Conflict(MESSAGE_DISPATCH_NO_REVIEW)
        quote = shipment.quote
        estimate = None
        if quote is not None and quote.method == chosen:
            # on the offer's basis (claims/info prices include VAT; check-price may not: the same uplift as
            # the customer fee, without the markup or the 500 so'm rounding)
            estimate = delivery_pricing.customer_fee(
                quote.carrier_price,
                markup_percent=Decimal(0),
                vat_percent=delivery_selectors.settings_row().estimate_vat_percent,
                step=1,
            )
        try:
            claim_pk = delivery_services.start_dispatch(
                order.pk,
                method=chosen,
                taxi_class=config.taxi_class,
                weight_g=weight_g,
                quoted_fee=order.delivery_fee,
                estimate_price=estimate,
                approved_price=approved_price,
                actor_id=actor_id,
                at=now,
            )
        except delivery_services.DispatchConflict as exc:
            raise Conflict(MESSAGE_DISPATCH_ACTIVE) from exc
        claim = delivery_selectors.claim_row(claim_pk)
        assert claim is not None  # noqa: S101 - just created in this transaction
        outbox.enqueue(
            topics.YANDEX_DISPATCH,
            {"order_id": order.pk, "courier_claim_id": claim_pk, "request_id": claim.request_id},
            dedupe_key=topics.yandex_dispatch_key(claim.request_id),
        )
        for kind in DISPATCH_RESOLVES:
            delivery_services.resolve_kind(order.pk, kind, RESOLVED_BY_DISPATCH_NOTE)
        _audit(
            AUDIT_DISPATCH_REQUESTED,
            order,
            actor_id,
            request,
            {
                "claim_number": claim.number,
                "request_id": claim.request_id,
                "method": chosen,
                "taxi_class": config.taxi_class,
                "weight_g": weight_g,
                "approved_price": approved_price,
            },
        )
        logger.info(
            "yandex.dispatch_requested %s",
            {"order_id": order.pk, "claim_number": claim.number, "method": chosen},
        )
        return claim_pk


@dataclass(frozen=True, slots=True)
class ClaimApplied:
    """What :func:`apply_yandex_claim` did (``outcome`` as ``delivery.services.SNAPSHOT_*``)."""

    outcome: str
    status: str = ""
    stage: str = ""
    order_status: str = ""
    order_changed: bool = False
    claim_pk: int | None = None
    needs_tracking_link: bool = False

    @property
    def applied(self) -> bool:
        return self.outcome == delivery_services.SNAPSHOT_APPLIED


def _yandex_transition(order: Order, target: str, carrier_status: str, via: str, at: datetime) -> bool:
    """Rows 11–12 for trigger ``yandex_status`` (event source ``yandex``, push); a terminal order never
    moves (no row leaves ``delivered`` / ``cancelled``)."""
    row = state.find_transition(
        order.status, target, state.Trigger.YANDEX_STATUS, fulfilment=order.fulfilment
    )
    if row is None:
        return False
    _set_status(order, target, at)
    event = record_event(
        order,
        row.event_type,
        {"via": via, "bts_status": carrier_status, "push_sent": push_sent(order.customer_id)},
        source=_SRC.YANDEX,
        at=at,
    )
    _enqueue_push(order, target, event)
    return True


def _failure_code(update: delivery_services.ClaimUpdate, snapshot: Any) -> str:
    """The ``dispatch_failed`` code of a claim that ended before pickup: Yandex's reason of
    ``estimating_failed``; ``offer_expired`` for an offer we confirmed that Yandex then failed (FAQ 14) or
    that Yandex says expired; else the status itself."""
    errors = tuple(snapshot.error_codes)
    if update.status == "estimating_failed":
        return errors[0][:64] if errors else update.status
    if update.status == "failed" and update.accept_sent and not update.accepted:
        return "offer_expired"
    if "offer_expired" in errors:
        return "offer_expired"
    return update.status


def _claim_effects(
    order: Order, update: delivery_services.ClaimUpdate, snapshot: Any, via: str, at: datetime
) -> bool:
    """The order side of a live claim's new status (plan §3.7); returns whether the order status moved."""
    status, stage = update.status, update.stage
    details = {"claim_number": update.claim_number}
    asked = order.status == _S.CANCELLED or update.cancel_requested  # a cancel / return we requested
    moved = False
    if update.accepted_now:
        record_event_once(
            order,
            _E.SHIPMENT_CREATED,
            {"tracking_number": "", "tariff": update.tariff, "manual": update.claim_number > 1},
            actor_id=update.requested_by_id,
            at=at,
        )
    if update.accepted_now or update.finished_now:  # Yandex settled what a dispatch left uncertain
        delivery_services.resolve_kind(
            order.pk, enums.DeliveryIssueKind.DISPATCH_UNKNOWN, RESOLVED_BY_STATUS_NOTE
        )
    if status in delivery_yandex.DELIVERED_STATUSES:
        if order.status == _S.CANCELLED:
            open_delivery_issue(
                order.pk,
                enums.DeliveryIssueKind.PARCEL_UNACCOUNTED,
                "delivered_after_cancel",
                urgent=True,
                details=details,
            )
        else:
            moved = _yandex_transition(order, _S.DELIVERED, status, via, at)
            if order.status == _S.DELIVERED:
                for kind in DELIVERED_RESOLVES:
                    delivery_services.resolve_kind(order.pk, kind, RESOLVED_BY_STATUS_NOTE)
        return moved
    if delivery_yandex.is_picked_up(status) and order.status == _S.PROCESSING:
        moved = _yandex_transition(order, _S.HANDED_TO_COURIER, status, via, at)
    if stage == enums.ShipmentStage.RETURNING and not asked:
        reason = (snapshot.return_reasons[0] if snapshot.return_reasons else "")[:64]
        record_event_once(
            order,
            _E.DELIVERY_FAILED,
            {
                "provider": enums.DeliveryProvider.YANDEX,
                "carrier_status": status,
                "reason": reason,
                "claim_id": update.claim_id,
            },
            at=at,
        )
        open_delivery_issue(
            order.pk, enums.DeliveryIssueKind.DELIVERY_FAILED, reason or status, details=details
        )
    if status in delivery_yandex.RETURNED_STATUSES:
        record_event_once(
            order,
            _E.SHIPMENT_RETURNED,
            {
                "provider": enums.DeliveryProvider.YANDEX,
                "carrier_status": status,
                "claim_id": update.claim_id,
            },
            at=at,
        )
        if not delivery_selectors.has_open_issue(
            order.pk, enums.DeliveryIssueKind.RETURN_PENDING
        ) and not delivery_selectors.return_confirmed(order.pk, update.claim_number):
            open_delivery_issue(order.pk, enums.DeliveryIssueKind.RETURN_PENDING, "returned", details=details)
    if status in delivery_yandex.FAILURE_STATUSES:
        if update.picked_up:
            open_delivery_issue(
                order.pk, enums.DeliveryIssueKind.PARCEL_UNACCOUNTED, status, urgent=True, details=details
            )
        elif not asked:
            parked = parked_stage(order, enums.ShipmentStage.FAILED)
            if stage != parked:
                delivery_services.set_dispatch_stage(order.pk, update.claim_pk or 0, stage=parked, at=at)
            open_delivery_issue(
                order.pk,
                enums.DeliveryIssueKind.DISPATCH_FAILED,
                _failure_code(update, snapshot),
                details=details,
            )
    return moved


def apply_yandex_claim(order_id: int, snapshot: Any, *, source: str) -> ClaimApplied:
    """The single writer of Yandex claim statuses (plan §3.7): a ``Claim`` snapshot from ``claims/info`` /
    ``bulk_info`` / ``create`` (``source``: ``dispatch``, ``poll``, ``callback``, ``dev``), under the Order
    lock.

    ``delivery.services.record_claim_snapshot`` updates the claim row, the shipment's stage / state and cost
    and appends the ``claim_status`` event — or ignores a stale or duplicate snapshot. For the current, open
    claim the order follows (``state.py`` rows 11–12, trigger ``yandex_status``, push): any picked-up status
    moves a ``processing`` order to ``handed_to_courier``, ``delivered*`` to ``delivered``; ``returning``
    records ``delivery_failed`` once per claim and opens the ``delivery_failed`` issue (Yandex's return
    reason as code); ``returned*`` records ``shipment_returned`` and keeps / opens ``return_pending``; a
    claim that fails before pickup (``performer_not_found``, ``cancelled_by_taxi``, ``failed``,
    ``estimating_failed``, a cancel we did not ask for) → stage ``failed`` + ``dispatch_failed``; any
    cancel / failure after pickup, and ``delivered*`` of a cancelled order, → urgent ``parcel_unaccounted``.
    Order terminal statuses never regress; an unknown status is stored and logged, nothing moves. A claim
    Yandex accepted that is not the one we follow → urgent ``dispatch_unknown``.
    """
    via = CLAIM_SOURCES[source]
    with transaction.atomic():
        order = lock_order(order_id)
        now = timeutils.now()
        update = delivery_services.record_claim_snapshot(
            order.pk, snapshot, via=via, paid_fee=order.delivery_fee, at=now
        )
        result = ClaimApplied(
            update.outcome,
            status=update.status,
            stage=update.stage,
            order_status=order.status,
            claim_pk=update.claim_pk,
        )
        if not update.applied:
            if update.outcome == delivery_services.SNAPSHOT_UNKNOWN_CLAIM:
                logger.warning("yandex.unknown_claim %s", {"order_id": order.pk, "source": source})
            return result
        if not update.known:
            logger.warning(
                "yandex.unknown_status %s",
                {"order_id": order.pk, "claim": update.claim_pk, "status": update.status},
            )
            return result
        if not update.live:
            if update.accepted_now:
                open_delivery_issue(
                    order.pk,
                    enums.DeliveryIssueKind.DISPATCH_UNKNOWN,
                    "unexpected_acceptance",
                    urgent=True,
                    details={"claim_number": update.claim_number},
                )
            return result
        moved = _claim_effects(order, update, snapshot, via, now)
        return ClaimApplied(
            update.outcome,
            status=update.status,
            stage=update.stage,
            order_status=order.status,
            order_changed=moved,
            claim_pk=update.claim_pk,
            needs_tracking_link=update.needs_tracking_link,
        )


# --- Yandex Delivery: carrier-aware cancel, returns (plan §3.8) ---------------------------------------------

AUDIT_CARRIER_CANCEL = "order.carrier_cancel"
AUDIT_RETURN_CONFIRMED = "order.return_confirmed"
MESSAGE_YANDEX_DELIVERED = N_("Buyurtma mijozga yetkazilgan: uni bekor qilib bo‘lmaydi.")
MESSAGE_YANDEX_COURIER_ACTIVE = N_(
    "Yandex kuryeri chaqirilgan: buyurtmani bekor qilishdan oldin kuryer buyurtmasi bekor qilinishi kerak."
)
MESSAGE_YANDEX_PARCEL_OUT = N_(
    "Posilka kuryerda, Yandex esa uni omborga qaytarayotganini bildirmagan: buyurtmani hozir bekor qilib "
    "bo‘lmaydi."
)
MESSAGE_YANDEX_REFUSED = N_(
    "Yandex kuryer buyurtmasini bekor qilmadi (kuryer posilkani olgan yoki bekor qilish muddati o‘tgan). "
    "Holatni tekshirib, qayta urinib ko‘ring."
)
MESSAGE_YANDEX_RETURN_REFUSED = N_(
    "Yandex posilkani omborga qaytarishni rad etdi. Yandex qo‘llab-quvvatlash xizmatiga murojaat qiling."
)
MESSAGE_YANDEX_RETURN_REQUESTED = N_(
    "Yandexdan posilkani omborga qaytarish so‘raldi. Buyurtma hali bekor qilinmadi: Yandex qaytarishni "
    "tasdiqlagach, bekor qilishni qayta bosing."
)
MESSAGE_YANDEX_CANCEL_PENDING = N_(
    "Yandex kuryer buyurtmasini bekor qilishni hali tasdiqlamadi. Birozdan so‘ng qayta urinib ko‘ring."
)
MESSAGE_YANDEX_BUSY = N_(
    "Bu buyurtma bo‘yicha Yandex bilan boshqa amal bajarilmoqda (kuryer chaqirilmoqda). Birozdan so‘ng "
    "qayta urinib ko‘ring."
)
MESSAGE_YANDEX_UNAVAILABLE = N_("Yandex javob bermadi, buyurtma bekor qilinmadi. Qayta urinib ko‘ring.")
MESSAGE_RETURN_NOT_CONFIRMABLE = N_("Qaytayotgan posilka yo‘q: qaytishni tasdiqlab bo‘lmaydi.")
MESSAGE_RETURN_CONFIRMED = N_("Posilka qaytgani allaqachon tasdiqlangan.")
RESOLVED_BY_CANCEL_NOTE = "Buyurtma bekor qilindi."
RESOLVED_BY_RETURN_NOTE = "Posilka omborga qaytdi."
RESOLVED_BY_STAFF_DELIVERED_NOTE = "Xodim topshirilganini tasdiqladi."
# The comment Yandex passes to the courier with ``claims/return`` (the API language is Russian / English).
RETURN_COMMENT = "Заказ отменён отправителем: верните посылку на склад."
# The 409 code of "the return was requested, Yandex has not confirmed it yet" (order unchanged).
CODE_RETURN_REQUESTED = "return_requested"
# A cancelled order no longer needs these (``return_pending`` and ``parcel_unaccounted`` stay: a parcel is
# still out there; ``dispatch_unknown`` closes with the claim; an ``incident`` is staff's).
CANCEL_RESOLVES: tuple[str, ...] = (
    enums.DeliveryIssueKind.DISPATCH_FAILED,
    enums.DeliveryIssueKind.DISPATCH_REVIEW,
    enums.DeliveryIssueKind.PREPARATION_OVERDUE,
    enums.DeliveryIssueKind.DELIVERY_FAILED,
    enums.DeliveryIssueKind.CANCEL_INCOMPLETE,
)
# What :func:`cancel_at_carrier` did at Yandex (``CarrierCancel.outcome``).
CARRIER_NOTHING = "nothing"  # no call: BTS / pickup / unpaid, no open claim, or the parcel returning / back
CARRIER_WITHDRAWN = "withdrawn"  # an open claim whose accept was never sent: closed (best-effort free cancel)
CARRIER_CANCELLED = "cancelled"  # Yandex cancelled the claim (now, or a cancel that crashed before the order)
CARRIER_RETURNING = "returning"  # the courier is bringing the parcel back at our request
# After claims/cancel or claims/return answered, Yandex's claim is read this often until it shows the change.
CARRIER_CONFIRM_READS = 3
CARRIER_CONFIRM_PAUSE_SECONDS = 1.0
_carrier_pause: Callable[[float], None] = time.sleep  # a module attribute so tests skip the pauses
# Recording that claims/cancel / claims/return was sent waits this long for the Order row (no flow holds it
# across a Yandex call) — generous, yet with the fallback below (≤ 20 s together) inside gunicorn's
# graceful_timeout (30 s): a deploy's restart that begins once Yandex answered still lets the marker commit
# before the worker is killed (a kill loses it like a crash).
CARRIER_RECORD_WAIT = timedelta(seconds=15)
# Past it, the marker alone is written without the Order lock, waiting at most this long for its own row.
CARRIER_MARK_WAIT = timedelta(seconds=5)

_WITHDRAW = "withdraw"
_CANCEL = "cancel"
_RETURN = "return"


@dataclass(frozen=True, slots=True)
class CarrierCancel:
    """What :func:`cancel_at_carrier` did before the order itself is cancelled."""

    outcome: str
    claim_number: int | None = None
    cancel_state: str = ""  # free | paid (claims/cancel-info)
    cancel_fee: int | None = None  # so'm, a paid cancellation


@dataclass(frozen=True, slots=True)
class _CarrierPlan:
    order_id: int
    shipment_id: int
    claim: delivery_selectors.ClaimRow
    scenario: dict[str, Any]
    paid_fee: int
    actor_id: int
    request: Any


def yandex_cancel_refusal(view: delivery_selectors.YandexCancelView | None) -> str | None:
    """Why a paid Yandex order cannot be cancelled **without a carrier call** right now (the Uzbek message),
    or ``None``: delivered → never; the parcel returning / back → yes (refund, no call); a claim still open
    → its courier must be cancelled first (:func:`cancel_at_carrier`); a parcel the courier took whose claim
    then ended without a return (``parcel_unaccounted``) → no refund until Yandex reports it coming back."""
    if view is None:
        return None
    if view.stage == enums.ShipmentStage.DELIVERED:
        return MESSAGE_YANDEX_DELIVERED
    if view.returning:
        return None
    if view.claim_open:
        return MESSAGE_YANDEX_COURIER_ACTIVE
    if view.picked_up:
        return MESSAGE_YANDEX_PARCEL_OUT
    return None


def _ensure_return_pending(order_id: int, claim_number: int | None, code: str) -> None:
    """Keep the physical return tracked until staff confirm it (an open ``return_pending`` is left as it is:
    the review beat may have made it urgent)."""
    if delivery_selectors.has_open_issue(order_id, enums.DeliveryIssueKind.RETURN_PENDING):
        return
    open_delivery_issue(
        order_id, enums.DeliveryIssueKind.RETURN_PENDING, code, details={"claim_number": claim_number}
    )


def cancel_at_carrier(order_id: int, actor_id: int, *, request: Any = None) -> CarrierCancel:
    """The carrier half of ``POST orders/{id}/cancel/`` for a Yandex order (plan §3.8), run by the view
    **before** its idempotent transaction (and skipped on a replay); :func:`cancel_order` follows.

    Held: the advisory lock ``yandex:{order_id}`` shared with the dispatch handler (busy → 409 ``conflict``).
    Txn A (Order lock, every refusal of :func:`cancel_order` first) decides and stamps
    ``Shipment.cancel_requested_at`` — from then on no dispatch sends an ``accept``; the Yandex calls run
    with no transaction open; txn B records the outcome (audit ``order.carrier_cancel``).

    * not Yandex, unpaid, no open claim, or the parcel returning / back → nothing at the carrier (a
      returning parcel keeps / opens ``return_pending``); delivered → 409 ``not_cancellable``;
    * an open claim whose ``accept`` was never sent summons nobody → best-effort free cancel at Yandex and
      the claim is closed here;
    * before pickup → fresh ``claims/info`` → ``cancel-info`` → ``claims/cancel(version, free|paid)``; once
      Yandex answered it — or its outcome is unknown — the claim is marked ``cancel_sent_at`` with the
      terms and a paid one's ``cancel-info`` price in its own short transaction (the fee goes on the claim
      and into the carrier cost at once when Yandex answered, else once a read or the poll shows
      ``cancelled_with_payment``; the Order lock waits ≤ :data:`CARRIER_RECORD_WAIT`, past it the marker
      alone is kept and the answer is 409 ``conflict``), then a re-read must show ``cancelled*``; terms
      ``unavailable`` / a 409 / a refusal → 409 ``not_cancellable``,
      Yandex not answering before anything was sent → 503 ``provider_unavailable`` (both clear the stamp);
      not visible yet, an unknown outcome included → 409 ``conflict`` (stamp kept: the review beat opens
      ``cancel_incomplete`` if Yandex then shows it cancelled);
    * after pickup → ``claims/return(point B)``, marked sent (``return_requested`` event) the same way → a
      re-read showing ``returning…`` lets the cancel proceed (refund) with ``return_pending`` open until the
      parcel is confirmed back; still in transit, an unknown outcome included → 409 ``return_requested``
      (order unchanged, ``return_pending`` open, stamp kept); refused → 409 ``not_cancellable``; not
      processed → 503;
    * a retry of a cancel / return that was sent → never a second ``cancel-info`` / ``claims/cancel`` /
      ``claims/return``: the claim is only read again (shown → proceeds; else the same 409, the stamp kept
      with its first time — a failed read never clears it);
    * a claim Yandex already shows cancelled (a cancel that crashed before the order was cancelled) → no
      second call.

    Every snapshot goes through :func:`apply_yandex_claim`; logs and audits carry ids and codes only.
    """
    if delivery_selectors.shipment_provider(order_id) != enums.DeliveryProvider.YANDEX:
        return CarrierCancel(CARRIER_NOTHING)
    try:
        with locks.yandex_lock(order_id):
            return _cancel_at_carrier(order_id, actor_id, request)
    except locks.YandexBusy as exc:
        raise Conflict(MESSAGE_YANDEX_BUSY) from exc


def _cancel_at_carrier(order_id: int, actor_id: int, request: Any) -> CarrierCancel:
    plan = _plan_carrier_cancel(order_id, actor_id, request)
    if isinstance(plan, CarrierCancel):
        return plan
    port = get_yandex_delivery(plan.scenario)
    sent = _carrier_call_sent(plan)
    try:
        snapshot = port.claim(plan.claim.claim_id)
    except YandexUnavailable as exc:
        if sent is not None:  # our cancel / return is with Yandex: a failed read never undoes it
            _carrier_call_pending(plan, sent)
        _carrier_refused(plan, "unavailable", ProviderUnavailable(MESSAGE_YANDEX_UNAVAILABLE), exc)
    except YandexError as exc:
        if sent is not None:
            _carrier_call_pending(plan, sent)
        if plan.claim.accept_sent_at is None:  # Yandex does not know a claim nobody accepted
            return _withdraw_claim(plan, port, None)
        _carrier_refused(plan, "refused", NotCancellable(MESSAGE_YANDEX_REFUSED), exc)
    apply_yandex_claim(order_id, snapshot, source="cancel")
    status = snapshot.status
    if status in delivery_yandex.DELIVERED_STATUSES:
        _carrier_refused(plan, "delivered", NotCancellable(MESSAGE_YANDEX_DELIVERED))
    if delivery_yandex.stage_for(status) in delivery_yandex.RETURN_STAGES:
        return _carrier_returning(plan, snapshot, requested=sent == _RETURN)
    picked_up = delivery_selectors.claim_picked_up(plan.shipment_id, plan.claim.claim_id, status)
    if delivery_yandex.is_finished(status):
        if picked_up:  # gone after pickup without a return: parcel_unaccounted, never refunded here
            _carrier_refused(plan, "parcel_out", NotCancellable(MESSAGE_YANDEX_PARCEL_OUT))
        if status in delivery_services.OUR_CANCEL_STATUSES:  # a fee our sent cancel left uncounted counts now
            return _carrier_already_cancelled(plan, status)
        _record_carrier(plan, "already_finished", status=status)
        return CarrierCancel(CARRIER_NOTHING, plan.claim.number)
    if status in delivery_yandex.PRE_ACCEPT and plan.claim.accept_sent_at is None:
        return _withdraw_claim(plan, port, snapshot)
    if picked_up:
        if sent == _RETURN:  # asked before (409 return_requested / a lost answer): only read again
            return _carrier_return_read(port, plan)
        return _carrier_return(port, plan, snapshot)
    if sent == _CANCEL:  # asked before (409 conflict / a lost answer): only read again
        latest = _carrier_reread(port, plan, delivery_yandex.CANCELLED_STATUSES)
        return _carrier_cancelled(plan, latest, plan.claim.cancel_state, plan.claim.cancel_fee)
    return _carrier_cancel(port, plan, snapshot)


def _carrier_call_sent(plan: _CarrierPlan) -> str | None:
    """The carrier call an earlier attempt of this cancel already sent for the claim — Yandex answered it,
    or its outcome is unknown (:data:`_RETURN`: a ``return_requested`` event, :data:`_CANCEL`:
    ``cancel_sent_at``) — or ``None``. Such a call is never sent again (a duplicate Yandex refuses must not
    clear the stamp of the first): a retry only reads the claim."""
    if delivery_selectors.return_requested(plan.shipment_id, plan.claim.claim_id):
        return _RETURN
    if plan.claim.cancel_sent_at is not None:
        return _CANCEL
    return None


def _carrier_call_pending(plan: _CarrierPlan, sent: str) -> NoReturn:
    """Yandex has our cancel / return but does not show it (yet): the same 409 as when it was sent — the
    stamp stays (no dispatch; the writer reads the coming ``cancelled*`` / ``returning`` as ours)."""
    if sent == _RETURN:
        _return_pending(plan, None)
    _cancel_pending(plan, plan.claim.cancel_state, plan.claim.cancel_fee)


def _plan_carrier_cancel(order_id: int, actor_id: int, request: Any) -> _CarrierPlan | CarrierCancel:
    """Txn A under the Order lock: refusals first, then the stamp (committed before any Yandex call)."""
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        view = delivery_selectors.yandex_cancel_view(order.pk)
        if not state.can_cancel(order.status):
            delivered = view is not None and order.status == _S.DELIVERED
            raise NotCancellable(MESSAGE_YANDEX_DELIVERED if delivered else MESSAGE_NOT_CANCELLABLE)
        if view is None or order.status not in (_S.PROCESSING, _S.HANDED_TO_COURIER):
            return CarrierCancel(CARRIER_NOTHING)  # unpaid: cancel_order releases what it holds
        if view.stage == enums.ShipmentStage.DELIVERED:
            raise NotCancellable(MESSAGE_YANDEX_DELIVERED)
        claim = view.claim
        if not view.claim_open or view.returning:
            refusal = yandex_cancel_refusal(view)
            if refusal is not None:
                raise NotCancellable(refusal)
            if view.returning and not view.return_confirmed:  # the return of the claim that carried it
                carried = view.return_claim or claim
                _ensure_return_pending(
                    order.pk, carried.number if carried is not None else None, str(view.stage)
                )
            return CarrierCancel(CARRIER_NOTHING, claim.number if claim is not None else None)
        if claim is None or not claim.is_open:  # the open claim is always the current one
            raise NotCancellable(MESSAGE_YANDEX_COURIER_ACTIVE)
        now = timeutils.now()
        delivery_services.request_carrier_cancel(order.pk, at=now)  # a repeated cancel keeps the first time
        plan = _CarrierPlan(
            order_id=order.pk,
            shipment_id=view.shipment_id,
            claim=claim,
            scenario=fake_scenario(order, "yandex"),
            paid_fee=order.delivery_fee,
            actor_id=actor_id,
            request=request,
        )
        if not claim.claim_id:  # claims/create never answered: no id, no accept, nobody summoned
            delivery_services.withdraw_claim(order.pk, claim.id, at=now)
            _audit_carrier(order, plan, "withdrawn")
            return CarrierCancel(CARRIER_WITHDRAWN, claim.number)
        return plan


def _audit_carrier(order: Order, plan: _CarrierPlan, outcome: str, **extra: Any) -> None:
    after = {"outcome": outcome, "claim_number": plan.claim.number, **extra}
    _audit(AUDIT_CARRIER_CANCEL, order, plan.actor_id, plan.request, after)
    logger.info("yandex.carrier_cancel %s", {"order_id": order.pk, **after})


def _record_carrier(plan: _CarrierPlan, outcome: str, **extra: Any) -> None:
    """Txn B: the carrier outcome (audit); the stamp stays (the order cancel follows, or Yandex finishes)."""
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        _audit_carrier(order, plan, outcome, **extra)


def _carrier_refused(
    plan: _CarrierPlan, outcome: str, error: Exception, cause: YandexError | None = None
) -> NoReturn:
    """Txn B of a refusal: the stamp goes (the order carries on as it was), the outcome is audited, the
    API error raised."""
    code = delivery_services.yandex_failure_code(cause) if cause is not None else ""
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        delivery_services.clear_carrier_cancel(order.pk)
        _audit_carrier(order, plan, outcome, error_code=code)
    raise error


def _carrier_reread(port: YandexDeliveryPort, plan: _CarrierPlan, wanted: Collection[str]) -> Claim | None:
    """Read the claim until its status is in ``wanted`` (a few reads, short pauses); every snapshot goes
    through the writer. The last snapshot read (``None`` when Yandex did not answer)."""
    latest: Claim | None = None
    for attempt in range(CARRIER_CONFIRM_READS):
        if attempt:
            _carrier_pause(CARRIER_CONFIRM_PAUSE_SECONDS)
        try:
            latest = port.claim(plan.claim.claim_id)
        except YandexError as exc:
            logger.info(
                "yandex.carrier_reread_failed %s", {"order_id": plan.order_id, "error": type(exc).__name__}
            )
            return latest
        apply_yandex_claim(plan.order_id, latest, source="cancel")
        if latest.status in wanted:
            return latest
    return latest


def _withdraw_claim(plan: _CarrierPlan, port: YandexDeliveryPort, snapshot: Claim | None) -> CarrierCancel:
    """An open claim nobody accepted: best-effort free cancel at Yandex (it keeps Yandex's list clean), then
    the claim is closed here — it can summon nobody, the stamp keeps any dispatch from accepting it."""
    if snapshot is not None:
        try:
            terms = port.cancel_terms(snapshot.claim_id)
            if terms.state == CANCEL_FREE:
                port.cancel(snapshot.claim_id, snapshot.version, CANCEL_FREE)
                _carrier_reread(port, plan, delivery_yandex.CANCELLED_STATUSES)
        except YandexError as exc:
            logger.info(
                "yandex.cancel_unaccepted_failed %s", {"order_id": plan.order_id, "error": type(exc).__name__}
            )
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        delivery_services.withdraw_claim(order.pk, plan.claim.id, at=timeutils.now())
        _audit_carrier(order, plan, "withdrawn")
    return CarrierCancel(CARRIER_WITHDRAWN, plan.claim.number)


def _carrier_cancel(port: YandexDeliveryPort, plan: _CarrierPlan, snapshot: Claim) -> CarrierCancel:
    """Before pickup: the terms, the cancel (free or paid) — recorded as sent with its terms and the paid
    price (:func:`_record_cancel_sent`) the moment Yandex answered it or its outcome became unknown, so it is
    never sent twice — then a re-read that must show ``cancelled*``. Only a call that provably changed
    nothing clears the stamp: ``cancel-info`` failing or refusing, terms ``unavailable``, ``claims/cancel``
    not processed or refused (unless Yandex shows the claim cancelled all the same)."""
    try:
        terms = port.cancel_terms(snapshot.claim_id)
    except YandexUnavailable as exc:
        _carrier_refused(plan, "unavailable", ProviderUnavailable(MESSAGE_YANDEX_UNAVAILABLE), exc)
    except YandexError as exc:
        _carrier_refused(plan, "refused", NotCancellable(MESSAGE_YANDEX_REFUSED), exc)
    if terms.state == CANCEL_UNAVAILABLE:
        _carrier_refused(plan, "unavailable_terms", NotCancellable(MESSAGE_YANDEX_REFUSED))
    fee = terms.price if terms.state == CANCEL_PAID else None
    try:
        port.cancel(snapshot.claim_id, snapshot.version, terms.state)
    except YandexUnavailable as exc:
        if not exc.outcome_unknown:  # nothing reached Yandex
            _carrier_refused(plan, "unavailable", ProviderUnavailable(MESSAGE_YANDEX_UNAVAILABLE), exc)
        # the cancel may have been processed: never resent, the claim says what happened (a paid one's fee
        # counts once Yandex shows ``cancelled_with_payment``, whichever read or poll shows it)
        _record_cancel_sent(plan, terms, answered=False)
        fee = None
    except YandexError as exc:  # 409 (terms changed, old version) or a refusal: nothing was cancelled
        latest = _carrier_reread(port, plan, delivery_services.OUR_CANCEL_STATUSES)
        if latest is None or latest.status not in delivery_services.OUR_CANCEL_STATUSES:
            _carrier_refused(plan, "refused", NotCancellable(MESSAGE_YANDEX_REFUSED), exc)
        return _carrier_already_cancelled(plan, latest.status, quoted=fee)  # cancelled all the same
    else:
        _record_cancel_sent(plan, terms, answered=True)
    latest = _carrier_reread(port, plan, delivery_yandex.CANCELLED_STATUSES)
    return _carrier_cancelled(plan, latest, terms.state, fee)


def _record_cancel_sent(plan: _CarrierPlan, terms: CancelTerms, *, answered: bool) -> None:
    """Right after ``claims/cancel`` answered (``answered``) or its outcome became unknown, before any
    re-read, in its own short transaction: the cancel is marked sent with its terms and, for a paid one, the
    ``cancel-info`` price (``cancel_price``). The fee of a paid cancel Yandex answered is counted at once; one
    whose answer was lost is counted once Yandex shows ``cancelled_with_payment`` — by the single writer, or
    here should a poll have shown it before this mark — so neither a lagging ``claims/info``, a lost answer
    nor a busy order loses it (counted once; a later ``final_price`` replaces it).

    The Order lock waits at most :data:`CARRIER_RECORD_WAIT`; past it the marker alone is written without it
    (:func:`_mark_without_order`) and the answer is 409 ``conflict`` with the stamp kept: the next attempt
    finds the marker, only re-reads, and its txn B counts a fee left uncounted.

    Residual window (accepted, not closed): the marker is committed only after Yandex answered, so a crash
    or a worker kill between that answer and this commit — or both waits running out — leaves no marker
    (logged ``yandex.carrier_marker_lost`` when it is the waits). The next attempt then sends
    ``cancel-info`` / ``claims/cancel`` again; Yandex refusing that duplicate while ``claims/info`` still
    lags clears the stamp."""
    price = terms.price if terms.state == CANCEL_PAID else None
    now = timeutils.now()
    try:
        with transaction.atomic():
            order = lock_order(plan.order_id, wait=CARRIER_RECORD_WAIT)
            # the marker, committed only after Yandex answered: a crash before this commit loses it
            # (the residual window above)
            delivery_services.record_cancel_sent(
                order.pk, plan.claim.id, cancel_state=terms.state, price=price, at=now
            )
            if price is None:
                return
            paid_fee = order.delivery_fee
            if answered:
                delivery_services.record_cancel_fee(order.pk, plan.claim.id, price, paid_fee=paid_fee)
            else:  # a poll may have shown cancelled_with_payment before the price was stored
                delivery_services.count_cancel_fee(order.pk, plan.claim.id, paid_fee=paid_fee)
            return
    except Conflict as exc:  # only lock_order raises it here: the order stayed busy
        busy = exc
    _mark_without_order(
        plan,
        _CANCEL,
        lambda: delivery_services.record_cancel_sent(
            plan.order_id, plan.claim.id, cancel_state=terms.state, price=price, at=now
        ),
    )
    raise Conflict(MESSAGE_YANDEX_CANCEL_PENDING) from busy


def _mark_without_order(plan: _CarrierPlan, call: str, write: Callable[[], object]) -> None:
    """The Order row stayed locked past :data:`CARRIER_RECORD_WAIT`: the sent marker alone is written
    without it — the claim's own columns under its row lock (a cancel) or an insert-only event (a return);
    no flow holds either for long, and this wait is bounded too (:data:`CARRIER_MARK_WAIT`). Every cancel
    attempt holds the advisory lock, so no other one races it. Should this wait run out as well, the marker
    is lost as in a crash: logged, never silent."""
    fields = {"order_id": plan.order_id, "claim_number": plan.claim.number, "call": call}
    logger.warning("yandex.carrier_record_busy %s", fields)
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('lock_timeout')")
                row = cursor.fetchone()
            previous = str(row[0]) if row else "0"
            _set_lock_timeout(f"{max(1, int(CARRIER_MARK_WAIT.total_seconds() * 1000))}ms")
            write()
            _set_lock_timeout(previous)
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        logger.error("yandex.carrier_marker_lost %s", fields)


def _carrier_already_cancelled(
    plan: _CarrierPlan, status: str, *, quoted: int | None = None
) -> CarrierCancel:
    """Yandex shows the claim ``cancelled*`` although this attempt's own cancel did not just do it (an earlier
    attempt's cancel, or ours refused while the claim got cancelled all the same): txn B counts the fee of a
    paid one nobody counted yet — ``quoted``, the price this attempt's ``cancel-info`` gave, else the one
    stored when our cancel was sent — and audits ``already_cancelled``; the order cancel follows."""
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        fee = None
        if status == delivery_services.PAID_CANCEL_STATUS:
            fee = delivery_services.count_cancel_fee(
                order.pk, plan.claim.id, paid_fee=order.delivery_fee, quoted=quoted
            )
        extra = {"cancel_fee": fee} if fee is not None else {}
        _audit_carrier(order, plan, "already_cancelled", status=status, **extra)
    return CarrierCancel(CARRIER_CANCELLED, plan.claim.number, cancel_fee=fee)


def _carrier_cancelled(
    plan: _CarrierPlan, latest: Claim | None, cancel_state: str, fee: int | None
) -> CarrierCancel:
    """A sent cancel: Yandex shows ``cancelled*`` → the order cancel proceeds (txn B counts a paid fee no
    writer counted yet and audits it); not yet → 409 ``conflict`` with the stamp kept (a retry only reads
    again; the review beat opens ``cancel_incomplete`` if Yandex then shows it cancelled and nobody
    finishes the cancel). ``fee`` = the fee known counted so far (``None`` after a lost answer)."""
    if latest is None or latest.status not in delivery_yandex.CANCELLED_STATUSES:
        _cancel_pending(plan, cancel_state, fee)
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        if latest.status == delivery_services.PAID_CANCEL_STATUS:
            fee = delivery_services.count_cancel_fee(order.pk, plan.claim.id, paid_fee=order.delivery_fee)
        _audit_carrier(
            order, plan, "cancelled", cancel_state=cancel_state, cancel_fee=fee, status=latest.status
        )
    return CarrierCancel(CARRIER_CANCELLED, plan.claim.number, cancel_state, fee)


def _cancel_pending(plan: _CarrierPlan, cancel_state: str, fee: int | None) -> NoReturn:
    """Yandex has the cancel but does not show it yet: the stamp stays, nothing else changes."""
    _record_carrier(plan, "cancel_pending", cancel_state=cancel_state, cancel_fee=fee)
    raise Conflict(MESSAGE_YANDEX_CANCEL_PENDING)


_RETURN_STATUSES = frozenset(
    status
    for status, stage in delivery_yandex.STAGE_BY_STATUS.items()
    if stage in delivery_yandex.RETURN_STAGES
)


def _carrier_return(port: YandexDeliveryPort, plan: _CarrierPlan, snapshot: Claim) -> CarrierCancel:
    """After pickup: ``claims/return`` to point B's id — recorded as sent (a ``return_requested`` event) the
    moment Yandex answered it or its outcome became unknown, so it is never sent twice — then the re-read
    (:func:`_carrier_return_read`). Only a call that provably started nothing clears the stamp: not
    processed, or refused while Yandex shows no return."""
    point = snapshot.destination_point_id or plan.claim.destination_point_id
    if point is None:
        _carrier_refused(plan, "no_point", NotCancellable(MESSAGE_YANDEX_RETURN_REFUSED))
    try:
        port.request_return(snapshot.claim_id, point, RETURN_COMMENT)
    except YandexUnavailable as exc:
        if not exc.outcome_unknown:  # nothing reached Yandex
            _carrier_refused(plan, "unavailable", ProviderUnavailable(MESSAGE_YANDEX_UNAVAILABLE), exc)
        _record_return_sent(plan)  # the return may have been started: never resent, the claim says
    except YandexError as exc:  # not_allowed, state_mismatch, inactive claim: no return was started
        latest = _carrier_reread(port, plan, _RETURN_STATUSES)
        if latest is not None and latest.status in _RETURN_STATUSES:  # coming back all the same
            return _carrier_returning(plan, latest, requested=False)
        _carrier_refused(plan, "return_refused", NotCancellable(MESSAGE_YANDEX_RETURN_REFUSED), exc)
    else:
        _record_return_sent(plan)
    return _carrier_return_read(port, plan)


def _record_return_sent(plan: _CarrierPlan) -> None:
    """Right after ``claims/return`` answered (or its outcome became unknown), before any re-read, in its own
    short transaction: the ``return_requested`` marker (idempotent). The Order lock waits at most
    :data:`CARRIER_RECORD_WAIT`; past it the marker alone is written without it (:func:`_mark_without_order`)
    and the answer is 409 ``conflict`` with the stamp kept: the next attempt finds the marker, only re-reads
    and opens ``return_pending``. The residual crash window of :func:`_record_cancel_sent` applies here too
    (no marker: a duplicate ``claims/return`` Yandex refuses while ``claims/info`` lags clears the stamp)."""
    now = timeutils.now()
    try:
        with transaction.atomic():
            order = lock_order(plan.order_id, wait=CARRIER_RECORD_WAIT)
            # the marker, committed only after Yandex answered: a crash before this commit loses it
            delivery_services.record_return_request(order.pk, plan.claim.id, at=now)
            return
    except Conflict as exc:  # only lock_order raises it here: the order stayed busy
        busy = exc
    _mark_without_order(
        plan, _RETURN, lambda: delivery_services.record_return_request(plan.order_id, plan.claim.id, at=now)
    )
    raise Conflict(MESSAGE_YANDEX_RETURN_REQUESTED) from busy


def _carrier_return_read(port: YandexDeliveryPort, plan: _CarrierPlan) -> CarrierCancel:
    """A sent return (now, or by an earlier attempt): read until Yandex shows it — ``returning…`` → the
    cancel proceeds (refund) with ``return_pending`` open; delivered meanwhile → 409 ``not_cancellable``;
    else 409 ``return_requested`` (order unchanged, ``return_pending`` open, stamp kept)."""
    latest = _carrier_reread(port, plan, _RETURN_STATUSES | delivery_yandex.DELIVERED_STATUSES)
    if latest is not None and latest.status in delivery_yandex.DELIVERED_STATUSES:
        _carrier_refused(plan, "delivered", NotCancellable(MESSAGE_YANDEX_DELIVERED))
    if latest is not None and latest.status in _RETURN_STATUSES:
        return _carrier_returning(plan, latest, requested=True)
    _return_pending(plan, latest)


def _return_pending(plan: _CarrierPlan, latest: Claim | None) -> NoReturn:
    """Yandex has the return but does not show it yet: the order stays as it is, ``return_pending`` open."""
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        _ensure_return_pending(order.pk, plan.claim.number, CODE_RETURN_REQUESTED)
        _audit_carrier(order, plan, "return_requested", status=latest.status if latest is not None else "")
    raise Conflict(MESSAGE_YANDEX_RETURN_REQUESTED, code=CODE_RETURN_REQUESTED)


def _carrier_returning(plan: _CarrierPlan, snapshot: Claim, *, requested: bool) -> CarrierCancel:
    """The parcel is coming back (at our request, or Yandex's own return): the cancel proceeds with
    ``return_pending`` open until staff confirm the parcel back."""
    stage = str(delivery_yandex.stage_for(snapshot.status) or enums.ShipmentStage.RETURNING)
    with transaction.atomic():
        order = lock_order(plan.order_id, wait=ADMIN_LOCK_WAIT)
        if not delivery_selectors.return_confirmed(order.pk, plan.claim.number):
            _ensure_return_pending(order.pk, plan.claim.number, stage)
        _audit_carrier(order, plan, "returning" if requested else "already_returning", status=snapshot.status)
    return CarrierCancel(CARRIER_RETURNING, plan.claim.number)


def confirm_return(order_id: int, actor_id: int, *, note: str = "", request: Any = None) -> None:
    """``POST orders/{id}/confirm-return/`` (plan §3.8): staff confirm the parcel of a Yandex order whose
    stage is ``returning`` / ``returned`` is physically back at the warehouse.

    Under the Order lock (409 ``conflict`` when busy > 5 s): stage ``returned`` (if Yandex has not said so),
    ``returned_at``, ``return_confirmed_by``, shipment event ``returned``, order event ``shipment_returned``
    (source ``admin``, once per claim), the open ``return_pending`` resolved by the actor (audited
    ``delivery.issue_resolved``), audit ``order.return_confirmed``. A cancelled order also closes its failed
    delivery / cancel issues; an open order stays as it is (its cancel stamp is cleared: staff dispatch
    again or cancel). The return is the one of the claim that carried the parcel (``return_claim``), not of
    a later re-dispatch that summoned nobody. Not returning → 409 ``invalid_transition``; confirmed already
    → 409 ``conflict``."""
    text = note.strip()[: delivery_services.ISSUE_NOTE_MAX]
    with transaction.atomic():
        order = lock_order(order_id, wait=ADMIN_LOCK_WAIT)
        view = delivery_selectors.yandex_cancel_view(order.pk)
        if view is None or view.return_claim is None or not view.returning:
            raise InvalidTransition(MESSAGE_RETURN_NOT_CONFIRMABLE)
        if view.return_confirmed:
            raise Conflict(MESSAGE_RETURN_CONFIRMED)
        now = timeutils.now()
        cancelled = order.status == _S.CANCELLED
        delivery_services.confirm_return(
            order.pk, actor_id=actor_id, at=now, clear_cancel_request=not cancelled
        )
        claim = view.return_claim  # shipment_returned of that claim: deduplicated with Yandex's returned*
        record_event_once(
            order,
            _E.SHIPMENT_RETURNED,
            {
                "provider": enums.DeliveryProvider.YANDEX,
                "carrier_status": claim.status,
                "claim_id": claim.claim_id,
            },
            actor_id=actor_id,
            at=now,
        )
        for issue in delivery_selectors.open_issues(
            order_id=order.pk, kind=enums.DeliveryIssueKind.RETURN_PENDING
        ):
            delivery_services.resolve_issue(
                issue.id, actor_id=actor_id, note=text or RESOLVED_BY_RETURN_NOTE, request=request
            )
        if cancelled:
            for kind in CANCEL_RESOLVES:
                delivery_services.resolve_kind(order.pk, kind, RESOLVED_BY_RETURN_NOTE)
        _audit(
            AUDIT_RETURN_CONFIRMED,
            order,
            actor_id,
            request,
            {"claim_number": claim.number, "order_status": order.status, "note": text},
        )


# --- refunds (handlers, Payme confirmation, staff resolve/retry) --------------------------------------------

MESSAGE_REFUND_STILL_PAID = N_(
    "Click to‘lovni qaytarmagan. Avval mijozga qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan "
    "yakunlang yoki qaytarishni qayta urinib ko‘ring."
)
MESSAGE_REFUND_CLICK_FAKE = N_(
    "Click sinov rejimida: to‘lov holatini tekshirib bo‘lmaydi. Click ulanganidan keyin yakunlang."
)
MESSAGE_REFUND_NO_PAYDOC = N_(
    "Bu to‘lovda Click to‘lov raqami yo‘q, shuning uchun Click’dan holatini tekshirib bo‘lmaydi. Mijozga "
    "qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan yakunlang."
)
# The 409 code of "Click has not reversed the payment" (admin contract §1.6, §7 item 18): the UI offers the
# manual payout ("Qo‘lda to‘landi") on it.
CODE_CLICK_NOT_REVERSED = "click_not_reversed"
# The 409 codes of the two other resolve/retry refusals the Payments board tells apart (admin contract §1.6):
# no Click payment id (pay out by hand) and Click fake under the production settings (cannot be checked).
CODE_CLICK_NO_PAYDOC = "click_no_paydoc"
CODE_CLICK_FAKE_MODE = "click_fake_mode"
MANUAL_REASON_PAYME = enums.RefundManualReason.PAYME.value
MANUAL_REASON_NO_PAYDOC = enums.RefundManualReason.NO_PAYDOC.value
MANUAL_REASON_FAKE_MODE = enums.RefundManualReason.FAKE_MODE.value


def refund_manual_reason(payment: payment_selectors.PaymentRow) -> str | None:
    """Why a new refund of ``payment`` is ``manual_required`` (admin §4 ``refund_requested.manual_reason``):
    ``payme`` (Payme refunds are cabinet refunds), ``no_paydoc`` (a Click payment without ``click_paydoc_id``
    was never sent to Click with another id), ``fake_mode`` (the Click port is fake under the production
    settings: the fake would report a reversal that moved no money), ``uzum_fake`` (the same for Uzum);
    ``None`` = submitted to the provider (Click, Uzum)."""
    if payment.provider == enums.PaymentProvider.PAYME:
        return MANUAL_REASON_PAYME
    if payment.provider == enums.PaymentProvider.UZUM:
        # refunds go to Uzum's API with the payment's Uzum order id (its provider transaction id)
        if integration_modes.callbacks_refused("uzum"):
            return enums.RefundManualReason.UZUM_FAKE.value
        return None
    if payment.provider != enums.PaymentProvider.CLICK:
        raise ValueError(f"Unknown payment provider {payment.provider!r}")
    if not payment_selectors.click_paydoc_id(payment.provider_state):
        return MANUAL_REASON_NO_PAYDOC
    if integration_modes.callbacks_refused("click"):
        return MANUAL_REASON_FAKE_MODE
    return None


def refund_requested_data(
    refund: payment_services.RefundResult, payment: payment_selectors.PaymentRow
) -> dict[str, Any]:
    """``refund_requested`` data with ``manual_reason`` (null unless the refund is ``manual_required``): the
    reason stored on the refund (``Refund.manual_reason``), else the one the payment implies now."""
    manual = refund.state == enums.RefundState.MANUAL_REQUIRED
    reason = refund.manual_reason or refund_manual_reason(payment) or MANUAL_REASON_PAYME
    return {
        "refund_id": refund.refund_id,
        "amount": refund.amount,
        "provider": refund.provider,
        "manual": manual,
        "manual_reason": reason if manual else None,
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
    **outside any row lock**: ``True`` when Click reversed the payment (closed without a payout); still paid
    without ``manual_payout`` → 409 ``click_not_reversed`` ``MESSAGE_REFUND_STILL_PAID``; Click down → 503;
    fake Click under the production settings → 409 ``click_fake_mode`` ``MESSAGE_REFUND_CLICK_FAKE``. A
    payment without a Click payment id is never checked (Click cannot be asked about it): without
    ``manual_payout`` → 409 ``click_no_paydoc`` ``MESSAGE_REFUND_NO_PAYDOC``, never the "Click has not
    reversed" answer."""
    if integration_modes.callbacks_refused("click"):
        raise InvalidTransition(MESSAGE_REFUND_CLICK_FAKE, code=CODE_CLICK_FAKE_MODE)
    if not row.provider_payment_ref:
        if not manual_payout:
            raise Conflict(MESSAGE_REFUND_NO_PAYDOC, code=CODE_CLICK_NO_PAYDOC)
        return False
    order = Order.objects.get(pk=row.order_id)
    try:
        status = get_click(fake_scenario(order, "click")).payment_status(row.provider_payment_ref)
    except ClickUnavailable as exc:
        raise ProviderUnavailable() from exc
    if status == "reversed":
        return True
    if not manual_payout:
        raise InvalidTransition(MESSAGE_REFUND_STILL_PAID, code=CODE_CLICK_NOT_REVERSED)
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
    **Click** refund as ``succeeded`` (customer contract §2.13.0): first the guard (newest refund, payment
    still ``paid``, no other open or succeeded refund; else 409 ``MESSAGE_REFUND_NOT_RESOLVABLE``, Click never
    asked), then a fresh Click status check outside any lock (``reversed`` → closed; still ``paid`` → only
    with ``manual_payout``, else 409 ``click_not_reversed``; no Click payment id → not checked, only with
    ``manual_payout``, else 409 ``click_no_paydoc``; Click down → 503; fake Click under the production
    settings → 409 ``click_fake_mode``), then the guard again under the Order → Payment locks. The state is
    checked under the Order lock (two concurrent resolves: the second gets 409 ``invalid_transition``);
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
        # the guard first (read-only, re-checked under the locks below): a refund that can no longer be
        # closed is refused without asking Click
        if not _resolvable_failed(first):
            raise InvalidTransition(MESSAGE_REFUND_NOT_RESOLVABLE)
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
    refund of the order is open, or — for Click — the Click port is fake under the production settings (409
    ``click_fake_mode`` ``MESSAGE_REFUND_CLICK_FAKE``: the retry would skip the status check of a later
    resolve)."""
    _aware(at, "at")
    with transaction.atomic():
        order, row = _locked_refund_row(refund_id)
        if row.state != enums.RefundState.FAILED:
            raise InvalidTransition(MESSAGE_REFUND_NOT_RETRYABLE)
        if row.provider == enums.PaymentProvider.CLICK and integration_modes.callbacks_refused("click"):
            raise InvalidTransition(MESSAGE_REFUND_CLICK_FAKE, code=CODE_CLICK_FAKE_MODE)
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
                manual_reason=refund_manual_reason(payment) or "",
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
    "AUDIT_DISPATCH_REQUESTED",
    "CLAIM_SOURCES",
    "ClaimApplied",
    "apply_yandex_claim",
    "open_delivery_issue",
    "parked_stage",
    "request_dispatch",
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
    "settle_billz_reservations",
    "settle_order_reservations",
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


# --- Billz stock freshness (gap X-STOCK-FRESHNESS, decision GC-5; customer contract §2.5, §2.7.1)
# ---------------------------------------------------------------------

# One customer request re-reads its stale lines within this (AGENTS.md: 4 s on a customer request path).
STOCK_READ_BUDGET_SECONDS = 4.0
# The worker's re-read of what a pushed sale took (batch client profile).
SOLD_STOCK_READ_BUDGET_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class StockCheck:
    """What :func:`refresh_stale_stock` did: the stale products it re-read from Billz (``refreshed``; a
    barcode Billz does not know is written as stock 0) and those it could not read (``unread``: product id →
    the cached ``ProductStock.synced_at``, ``None`` when Billz was never read for it). ``deferred``: the stale
    products Billz did not reach although it was answering (the 4 s budget or the limiter wait ran out, an
    ambiguous barcode search) — they go on from the cache and never refuse a checkout (review fix)."""

    refreshed: frozenset[int] = frozenset()
    unread: Mapping[int, datetime | None] = field(default_factory=dict)
    deferred: frozenset[int] = frozenset()

    def refuses_checkout(self, now: datetime) -> bool:
        """Billz failed (not merely ran out of budget) for a line whose cache is older than
        ``BILLZ_STOCK_STALE_REFUSE_SECONDS``."""
        bound = now - timedelta(seconds=int(settings.BILLZ_STOCK_STALE_REFUSE_SECONDS))
        return any(synced_at is None or synced_at < bound for synced_at in self.unread.values())


FRESH = StockCheck()


def stock_freshness_on() -> bool:
    """The freshness re-read applies only while Billz is live and online selling is on (fake: no change)."""
    return billz_mode() == "live" and integration_modes.online_sales_enabled()


def refresh_stale_stock(
    product_ids: Collection[int],
    *,
    budget_seconds: float = STOCK_READ_BUDGET_SECONDS,
    interactive: bool = True,
    max_age: timedelta | None = None,
) -> StockCheck:
    """Re-read from Billz the stock of every product whose ``ProductStock.synced_at`` is older than
    ``max_age`` (default ``BILLZ_STOCK_MAX_AGE_SECONDS``) — never read first, then the oldest — through the
    Billz port (one lookup per barcode, all within ``budget_seconds``) and write it with
    ``catalog.services.sync_stock`` (each in its own short transaction; never backwards).

    Only while Billz is live and online selling is on (fake mode: nothing changes, :data:`FRESH`). **Must run
    outside every row lock and outside the request's transaction** (the cart and checkout views call it before
    their ``@idempotent`` transaction): the Billz call holds no lock, and each ``sync_stock`` commits at once,
    so a later ``503`` or rollback keeps what was read and no stock row lock is held ahead of the customer API
    lock order (Customer → Cart → ProductStock). Never raises for a Billz failure: what it could not read is
    :attr:`StockCheck.unread` (the caller decides: cart writes carry on from the cache, checkout may refuse).
    """
    if not stock_freshness_on():
        return FRESH
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return FRESH
    now = timeutils.now()
    age = max_age if max_age is not None else timedelta(seconds=int(settings.BILLZ_STOCK_MAX_AGE_SECONDS))
    bound = now - age
    stale: list[tuple[Product, datetime | None]] = []
    for product in Product.objects.filter(pk__in=ids).select_related("stock").order_by("pk"):
        stock = catalog_selectors.stock_of(product)
        synced_at = stock.synced_at if stock is not None else None
        if synced_at is None or synced_at < bound:
            stale.append((product, synced_at))
    if not stale:
        return FRESH
    stale.sort(key=lambda item: (item[1] is not None, item[1] or now, item[0].pk))
    read = catalog_billz.get_port().read_stock(
        [product.billz_id for product, _ in stale], budget_seconds=budget_seconds, interactive=interactive
    )
    refreshed: set[int] = set()
    unread: dict[int, datetime | None] = {}
    deferred: set[int] = set()
    for product, synced_at in stale:
        row = read.rows.get(product.billz_id)
        if row is not None:
            catalog_services.sync_stock(product, row.stock, row.synced_at)
            refreshed.add(product.pk)
        elif product.billz_id in read.unknown:
            # Billz no longer knows the barcode: nothing of it can be sold (its sale would be refused) — the
            # daily full sync hides the product
            logger.warning("billz.stock_unknown product_id=%s: stock read as 0", product.pk)
            catalog_services.sync_stock(product, 0, now)
            refreshed.add(product.pk)
        elif product.billz_id in read.deferred:
            deferred.add(product.pk)  # Billz answers but the budget ran out: the cache stands, no refusal
        else:
            unread[product.pk] = synced_at
    logger.info(
        "billz.stock_refreshed read=%d unread=%d deferred=%d", len(refreshed), len(unread), len(deferred)
    )
    return StockCheck(frozenset(refreshed), unread, frozenset(deferred))


def reread_sold_stock(product_ids: Collection[int]) -> StockCheck:
    """``billz.stock_reread`` (after a pushed sale): read the sold products from Billz again whatever their
    age (batch profile, a minute), then settle the sold units the new read reflects. Best effort: what Billz
    could not answer waits for the next delta or cart read (the unsettled units keep counting meanwhile)."""
    check = refresh_stale_stock(
        product_ids,
        budget_seconds=SOLD_STOCK_READ_BUDGET_SECONDS,
        interactive=False,
        max_age=timedelta(0),
    )
    if check.refreshed:
        settle_billz_reservations(sorted(check.refreshed))
    if check.unread or check.deferred:
        logger.info(
            "billz.stock_reread_incomplete unread=%s deferred=%s",
            sorted(check.unread),
            sorted(check.deferred),
        )
    return check


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


def reorder_into_cart(order_id: int, customer_id: int, *, preserve_all: bool = False) -> None:
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
            if preserve_all:
                raise CartFull(
                    details={"max_lines": settings.CART_MAX_LINES}, params={"limit": settings.CART_MAX_LINES}
                )
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
        if cart.last_checkout_order_id == order_id:
            cart.last_checkout_order_id = None
            cart.updated_at = now
            cart.save(update_fields=["last_checkout_order", "updated_at"])
        else:
            _touch(cart)


def restore_checkout_to_cart(order_id: int, customer_id: int) -> None:
    """Cancel an unpaid checkout and merge its items atomically, without losing a newer cart.

    Serialize against checkout, then use the existing Order → Payment → Loyalty locks and the existing
    Click-in-flight guard. A cart_full or other failure rolls the cancellation and released holds back.
    A previously expired/customer-cancelled checkout can be restored too; paid orders are never edited.
    """
    with transaction.atomic():
        customer = lock_customer(customer_id)
        if customer is None or customer.status != enums.CustomerStatus.ACTIVE:
            raise AuthFailed("SESSION_EXPIRED")
        order = lock_order(order_id)
        if order.customer_id != customer_id:
            raise OrderNotFound(f"Order {order_id} does not exist")
        if order.status == _S.AWAITING_PAYMENT:
            cancel_by_customer(order_id, customer_id)
        elif order.status != _S.CANCELLED or order.cancellation_reason not in (
            enums.CancellationReason.RESERVATION_EXPIRED,
            enums.CancellationReason.CUSTOMER_REQUEST,
        ):
            raise not_cancellable("order")
        reorder_into_cart(order_id, customer_id, preserve_all=True)


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
    # a Yandex delivery quote (plan §3.4) the view checked or re-quoted before the transaction; None = BTS
    delivery_quote_id: UUID | None = None


def _lock_yandex_quote(customer_id: int, quote_id: UUID, address_id: int) -> delivery_quotes.QuoteRow:
    """Checkout's transaction (no network): lock the quote and check it again against the saved address as it
    is now and the method settings; anything off raises :class:`quoting.QuoteExpired` (409
    ``quote_expired``)."""
    now = timeutils.now()
    saved = Address.objects.filter(pk=address_id, customer_id=customer_id).first()
    if saved is None:
        raise AddressNotFound(f"Unknown address {address_id}")
    snapshot = delivery_quotes.AddressSnapshot(
        address_id=saved.pk,
        text=saved.text,
        latitude=saved.latitude,
        longitude=saved.longitude,
        region_code=saved.region_code,
        city_code=saved.city_code,
    )
    try:
        quote = delivery_quotes.lock_for_checkout(
            quote_id, customer_id=customer_id, address=snapshot, now=now
        )
    except delivery_quotes.QuoteUnusable as exc:
        raise quoting.QuoteExpired() from exc
    if quote.provider != enums.DeliveryProvider.YANDEX or not quoting.method_usable(
        customer_id, quote.method, now
    ):
        raise quoting.QuoteExpired()
    return quote


def lock_customer(customer_id: int) -> Customer | None:
    """The customer-level lock of the customer API: ``SELECT … FOR NO KEY UPDATE`` (never ``FOR UPDATE``,
    which would deadlock with a capture's ``Purchase`` insert; customer contract §2.7.1)."""
    return Customer.objects.select_for_update(no_key=True, of=("self",)).filter(pk=customer_id).first()


def checkout(customer_id: int, data: CheckoutInput, *, stock: StockCheck = FRESH) -> int:
    """``POST orders/checkout/`` after the view's body, consent and address checks (rows 1–3): row 0 (online
    selling), row 4 (the BTS quote, no row lock held), row 4a (the Billz stock freshness of the lines:
    ``stock`` is what the view's :func:`refresh_stale_stock` could not read before the transaction — a line
    Billz could not confirm whose cache is older than ``BILLZ_STOCK_STALE_REFUSE_SECONDS`` → 503
    ``provider_unavailable``), then the customer-level lock (``FOR NO KEY UPDATE``; an inactive customer → 401
    ``SESSION_EXPIRED``), row 5 (open orders), row 6 (cart read under the lock and the Cart lock) and
    :func:`create_order` (rows 7–10); the cart is emptied and ``Cart.last_checkout_order`` set in the same
    transaction. Returns the order id."""
    _sales_on()
    delivery = data.fulfilment == enums.FulfilmentMethod.DELIVERY
    scenario = dict(data.scenario or {})
    yandex_quote = delivery and data.delivery_quote_id is not None
    if yandex_quote and data.address_id is None:
        raise CheckoutError("A Yandex quote needs a saved address")
    bts_scenario = scenario.get("bts") if isinstance(scenario.get("bts"), dict) else None
    # a Yandex quote was priced before the transaction (the view's post()); BTS is quoted here, no lock held
    fee = (
        quote_delivery(data.region_code, data.city_code, bts_scenario) if delivery and not yandex_quote else 0
    )
    if stock.refuses_checkout(timeutils.now()):
        logger.warning(
            "checkout.stock_unconfirmed customer_id=%s products=%s", customer_id, sorted(stock.unread)
        )
        raise ProviderUnavailable()
    with transaction.atomic():
        customer = lock_customer(customer_id)
        if customer is None or customer.status != enums.CustomerStatus.ACTIVE:
            raise AuthFailed("SESSION_EXPIRED")
        quote = None
        if yandex_quote:
            # under the customer lock, before anything else: a quote lost to a concurrent checkout of the same
            # customer is 409 quote_expired (not the emptied cart)
            assert data.delivery_quote_id is not None and data.address_id is not None  # noqa: S101
            quote = _lock_yandex_quote(customer_id, data.delivery_quote_id, data.address_id)
            fee = quote.customer_fee
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
        if quote is not None and quote.weight_g != quoting.parcel_weight_g(
            sum(int(quantity) for _, quantity in items)
        ):
            raise quoting.QuoteExpired()  # the cart changed after the quote was checked
        metadata: dict[str, Any] = {"checkout": dict(data.checkout_meta or {})}
        if scenario:
            metadata["fake"] = scenario
        shipment: dict[str, Any] = {}
        if quote is not None:
            shipment = {
                "delivery_provider": enums.DeliveryProvider.YANDEX,
                "delivery_method": quote.method,
                "delivery_quote_id": quote.id,
            }
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
            **shipment,
        )
        if quote is not None:
            try:
                delivery_quotes.mark_used(quote.id, order_id=order_id, at=timeutils.now())
            except delivery_quotes.QuoteUnusable as exc:  # pragma: no cover - the row is locked above
                raise quoting.QuoteExpired() from exc
        CartItem.objects.filter(cart=cart).delete()
        cart.last_checkout_order_id = order_id
        cart.save()
        return order_id


# --- pay (customer contract §2.7.2)
# --------------------------------------------------------------------------------


def ensure_payable_payment(
    order_id: int,
    provider: str,
    *,
    superseding: str | None = None,
    renew: bool = False,
    at: datetime | None = None,
) -> payment_selectors.PaymentRow:
    """The payment a provider page / callback works on, under ``lock_order`` → Payment locks.

    Same provider → the active ``pending`` payment is reused as it is (nothing written); with
    ``superseding=<click_trans_id>`` (Click Prepare only) a Click payment attached to **another** transaction
    is failed ``abandoned`` (no event) and a new ``pending`` row is created; with ``renew=True`` (Uzum only:
    the payment's form is spent — its session is over, Uzum refused the order number, or the attempt's
    outcome is unknown) the Uzum row is failed ``abandoned`` and replaced the same way, never while its form
    may still take the money (:class:`PaymentInProgress`); a Payme row is reused only while
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
            ) or (renew and provider == enums.PaymentProvider.UZUM)
            if not superseded:
                return pending
            result = payment_services.fail_payment(pending.id, "abandoned", now, unilateral=True)
            if not result.changed:  # an Uzum form that may still take the money is never replaced
                raise PaymentInProgress(
                    details={"provider": pending.provider, "order_id": order_id},
                    params={"provider": selectors.provider_brand(pending.provider)},
                )
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
    (``error.order_not_cancellable``); a **Click** or **Uzum** payment in flight → 409
    ``payment_in_progress``. The pending payment fails unilaterally (``cancelled``; a Payme state-1
    transaction stays ``pending`` until Payme's ``CancelTransaction``), reservations and the hold are
    released, ``cancelled`` / ``customer_request`` with ``cancelled_by`` null, event ``cancelled`` (source
    system), push ``cancelled``. No audit."""
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
            and pending.provider in (enums.PaymentProvider.CLICK, enums.PaymentProvider.UZUM)
            and in_flight(pending, instant)
        ):
            raise PaymentInProgress(
                details={"provider": pending.provider, "order_id": order.pk},
                params={"provider": selectors.provider_brand(pending.provider)},
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
    {order_id, payment_id, provider, case}``, ERROR ``payment.late_capture`` and, after the commit, the alert
    of the same name (scope: the payment). The order never changes status. ``None`` when the payment was
    already recorded as a late capture (a replay)."""
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
            manual_reason=refund_manual_reason(row) or "",
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
        # ids and short codes only; once the refund is committed, one alert per payment
        transaction.on_commit(
            lambda: alerts.alert("payment.late_capture", details, scope=str(payment_id)), robust=True
        )
        return refund
