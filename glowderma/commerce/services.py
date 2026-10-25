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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from glowderma.accounts.models import Address, Customer, Device, StaffUser
from glowderma.catalog.models import Product, ProductStock
from glowderma.commerce import events, selectors, state, topics
from glowderma.commerce.models import (
    EVENT_SOURCES,
    EVENT_STATUS_AFTER,
    Order,
    OrderEvent,
    OrderItem,
    StockReservation,
)
from glowderma.core import audit, enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict, InvalidTransition, NotCancellable, ProviderUnavailable
from glowderma.core.media import absolute_media_url
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts.ports import BtsUnavailable
from glowderma.integrations.bts.registry import get_bts
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services
from glowderma.payments.models import OPEN_REFUND_STATES
from glowderma.site_config.models import SiteSettings

MESSAGE_NOT_CANCELLABLE = "Bu buyurtmani bekor qilib bo‘lmaydi."
MESSAGE_BTS_REFUSED = "BTS buyurtmani bekor qilmadi: {reason}."
MESSAGE_INVALID_TRANSITION = "Bu holatga o‘tib bo‘lmaydi."
MESSAGE_RETRY_REFUSED = "BTS ga qayta yuborib bo‘lmaydi."
MESSAGE_RETRY_PENDING = "BTS so‘rovi allaqachon yuborilmoqda."
MESSAGE_REFUND_NOT_RESOLVABLE = "Bu qaytarishni yakunlab bo‘lmaydi."
MESSAGE_REFUND_NOT_RETRYABLE = "Bu qaytarishni qayta yuborib bo‘lmaydi."
MESSAGE_REFUND_OPEN = "Bu to‘lov uchun qaytarish allaqachon jarayonda."
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
    """``available_stock < quantity`` for these products (M2 ``OUT_OF_STOCK``)."""

    def __init__(self, product_ids: Sequence[int]) -> None:
        super().__init__(f"Out of stock: {sorted(product_ids)}")
        self.product_ids = tuple(sorted(product_ids))


# --- helpers ------------------------------------------------------------------------------------------------


def _aware(value: datetime | None, name: str) -> None:
    if value is not None and timezone.is_naive(value):
        raise ValueError(f"{name} must be timezone-aware")


def lock_order(order_id: int, *, skip_locked: bool = False) -> Order:
    """The order row ``SELECT … FOR UPDATE OF`` itself — the first lock of every flow."""
    qs = Order.objects.select_for_update(of=("self",), skip_locked=skip_locked).filter(pk=order_id)
    order = qs.first()
    if order is None:
        raise OrderNotFound(f"Order {order_id} does not exist")
    return order


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
    key = events.DEDUPE_KEYS.get(event_type)
    if key is None:
        return False
    return OrderEvent.objects.filter(
        order_id=order_id, type=event_type, **{f"data__{key}": data[key]}
    ).exists()


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


def _first_image_url(product: Product) -> str:
    image = product.images.order_by("position", "id").first()
    return absolute_media_url(image.file.name or "") if image is not None and image.file else ""


def create_order(
    customer_id: int,
    items: Sequence[tuple[int, int]],
    fulfilment: str,
    *,
    payment_provider: str,
    channel: str,
    address_id: int | None = None,
    address_text: str = "",
    region_code: str = "",
    city_code: str = "",
    address_comment: str = "",
    cashback_used: int = 0,
    at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    order_id: int | None = None,
    billz_order_uuid: UUID | None = None,
    enforce_stock: bool = True,
) -> int:
    """Checkout (03 §4.1 row 1): snapshot the lines, reserve stock, hold cashback → ``awaiting_payment``.

    ``items`` are ``(product_id, quantity)`` in cart order (one line per product). The delivery fee is the
    BTS tariff quote for the address (``0`` for pickup); ``cashback_used`` (≤ ``items_subtotal``) becomes a
    ``loyalty`` hold until the reservation expires. Stock is checked under the ``ProductStock`` row locks
    (:class:`OutOfStock`); ``enforce_stock=False`` is for the demo seeder only, whose orders are replayed at
    historical instants against today's Billz stock. ``order_id``/``billz_order_uuid`` pin design ids (seed
    only). Writes the ``Order``, items, reservations, the hold, the ``pending`` payment, the delivery
    shipment and the events ``order_created`` + ``reservation_created``; no outbox, no audit. Returns the id.
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
        if enforce_stock:
            list(
                ProductStock.objects.select_for_update(of=("self",))
                .filter(product_id__in=product_ids)
                .order_by("product_id")
            )
            available = selectors.available_stock(product_ids)
            short = [
                line.product.pk
                for line in lines
                if line.product.status != enums.ProductStatus.ACTIVE
                or available.get(line.product.pk, 0) < line.quantity
            ]
            if short:
                raise OutOfStock(short)
        items_subtotal = sum(line.product.price * line.quantity for line in lines)
        promo_subtotal = sum(line.product.price * line.quantity for line in lines if line.product.promo)
        if not 0 <= cashback_used <= items_subtotal:
            raise CheckoutError("cashback_used must be between 0 and the items subtotal")
        address = {"text": "", "region_code": "", "city_code": "", "comment": ""}
        delivery_fee = 0
        if fulfilment == enums.FulfilmentMethod.DELIVERY:
            if address_id is not None:
                saved = Address.objects.filter(pk=address_id, customer_id=customer_id).first()
                if saved is None:
                    raise CheckoutError(f"Unknown address {address_id}")
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
                raise CheckoutError("A delivery order needs an address")
            fake = meta.get("fake") if isinstance(meta.get("fake"), dict) else {}
            scenario = fake.get("bts") if isinstance(fake, dict) else None
            delivery_fee = get_bts(scenario if isinstance(scenario, dict) else None).quote(
                address["region_code"], address["city_code"]
            )
        total = items_subtotal + delivery_fee - cashback_used
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
                image_url=_first_image_url(line.product),
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
            loyalty_services.create_hold(customer.pk, order.pk, cashback_used, expires_at)
        payment_services.create_payment(order.pk, payment_provider, total)
        if fulfilment == enums.FulfilmentMethod.DELIVERY:
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


def mark_paid(order_id: int, provider_txn_id: str, at: datetime | None = None) -> None:
    """A provider capture (Click ``complete`` / Payme ``PerformTransaction``, M2 webhooks; the seed).

    Under Order → Payment → Account: the order's ``pending`` payment becomes ``paid``
    (``payments.services.confirm_payment``) and :func:`confirm_payment_effects` runs in the same transaction.
    A capture for an order that is no longer ``awaiting_payment`` is refused (``InvalidTransition``; M2 turns
    it into a late-capture refund). Idempotent for the same transaction on a paid order.
    """
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        order = lock_order(order_id)
        paid = payment_selectors.active_payment(order_id, enums.PaymentStatus.PAID)
        if order.status != _S.AWAITING_PAYMENT:
            if paid is not None and paid.transaction_id == provider_txn_id:
                return
            raise InvalidTransition(f"Order {order_id} is {order.status}, not awaiting payment")
        pending = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
        if pending is None:
            raise InvalidTransition(f"Order {order_id} has no pending payment")
        payment_services.confirm_payment(pending.id, provider_txn_id, instant)
        confirm_payment_effects(order_id, pending.id, instant)


def confirm_payment_effects(order_id: int, payment_id: int, at: datetime | None = None) -> None:
    """After-payment orchestration (03 §4.1 row 2) for an order whose payment is already ``paid``.

    Reservations ``active → converted`` (Billz owns ``ProductStock``; converted units count against
    availability until the next Billz sync, 03 §1.6), the cashback hold is captured (debit), the purchase and
    its credit are recorded under the policy active at ``at``, status ``processing``; events
    ``payment_confirmed`` (+ ``cashback_credited`` when the credit is positive); outbox ``billz.push_sale``
    (the Billz order UUID is persisted on the purchase first), ``bts.create_shipment`` for delivery orders
    and ``push.order_status processing``. No audit (system).
    """
    _aware(at, "at")
    instant = at or timeutils.now()
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
    order_id: int, error_code: str | None, at: datetime | None = None, *, provider_txn_id: str | None = None
) -> bool:
    """A provider failure callback (03 §4.1 row 16; Click ``complete`` error < 0, Payme ``CancelTransaction``
    on state 1): the pending payment fails and ``payment_failed`` is recorded; the order stays payable until
    expiry. Returns ``False`` when there is no pending payment (nothing written)."""
    _aware(at, "at")
    instant = at or timeutils.now()
    with transaction.atomic():
        order = lock_order(order_id)
        pending = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
        if pending is None:
            return False
        payment_services.fail_payment(pending.id, error_code or "failed", instant)
        record_event(
            order,
            _E.PAYMENT_FAILED,
            {
                "provider": pending.provider,
                "transaction_id": provider_txn_id or pending.transaction_id,
                "error_code": error_code,
            },
            at=instant,
        )
        return True


# --- reservation expiry (row 3) -----------------------------------------------------------------------------


def expire_reservation(order_id: int, at: datetime | None = None, *, skip_locked: bool = False) -> bool:
    """The reservation of an unpaid order ran out (03 §4.1 row 3): ``cancelled`` / ``reservation_expired``.

    Re-checked under the Order lock (a capture may have won): ``awaiting_payment`` and
    ``reservation_expires_at ≤ now``. The pending payment fails silently (unless it is a Payme state-1
    transaction, P-2), reservations are released, the hold is released (``reservation_expired``); event
    ``reservation_expired {released_item_count, payment_failed}``; no push, no audit. Returns whether the
    order was expired (``False`` = nothing to do, or locked by another transaction with ``skip_locked``).
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
        payment_failed = False
        pending = payment_selectors.active_payment(order_id, enums.PaymentStatus.PENDING)
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


def cancel_order(
    order_id: int, reason: str, actor_id: int, *, at: datetime | None = None, request: Any = None
) -> CancelResult:
    """``POST orders/{id}/cancel/`` (contract §2.2 table; 03 §4.1 rows 4/6/7).

    * ``awaiting_payment`` → the pending payment fails (Payme state 1 excepted), reservations and the hold
      are released;
    * ``processing`` / ``handed_to_courier`` → a BTS parcel that exists is cancelled **first**, synchronously
      (refusal → 409 ``not_cancellable`` with BTS's reason; transport failure → 503 ``provider_unavailable``;
      nothing is written before the answer); then a Refund (Click ``requested`` + outbox, Payme
      ``manual_required``; the payment stays ``paid``) and one reversal per unreversed debit/credit;
    * ``delivered`` / ``cancelled`` → 409 ``not_cancellable``.

    Effects: ``cancelled`` (reason ``admin``, note, ``cancelled_by``), events ``cancelled`` (+
    ``refund_requested``), push ``cancelled``, audit ``order.cancelled``.
    """
    _aware(at, "at")
    note = clean_reason(reason)
    with transaction.atomic():
        order = lock_order(order_id)
        if not state.can_cancel(order.status):
            raise NotCancellable(MESSAGE_NOT_CANCELLABLE)
        instant = at or timeutils.now()
        bts_cancelled = False
        if order.status in (_S.PROCESSING, _S.HANDED_TO_COURIER):
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
            paid = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PAID)
            if paid is not None:
                refund = payment_services.create_refund(
                    paid.id, paid.amount, actor_id, at=instant, request=request
                )
            purchase = loyalty_selectors.purchase_for_order(order.pk)
            if purchase is not None:
                reversal = loyalty_services.reverse_purchase(purchase.id, instant, actor_id)
                cashback_reversed = len(reversal.reversal_entry_ids) > 0
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
        if refund is not None:
            record_event_once(
                order,
                _E.REFUND_REQUESTED,
                {
                    "refund_id": refund.refund_id,
                    "amount": refund.amount,
                    "provider": refund.provider,
                    "manual": refund.state == enums.RefundState.MANUAL_REQUIRED,
                },
                actor_id=actor_id,
                at=instant,
            )
        _enqueue_push(order, _S.CANCELLED, cancelled)
        _audit(
            state.AUDIT_ORDER_CANCELLED,
            order,
            actor_id,
            request,
            {
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
        order = lock_order(order_id)
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
        order = lock_order(order_id)
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


def resolve_refund(
    refund_id: int, outcome: str, actor_id: int | None, *, at: datetime | None = None, request: Any = None
) -> payment_services.RefundResult:
    """Close a ``manual_required`` / ``unknown`` refund as ``succeeded`` or ``failed`` (contract §7 item 18
    ``…/resolve/``; with ``actor_id=None`` it stands for Payme's ``CancelTransaction``). Writes the
    ``refund_succeeded`` / ``refund_failed`` event under the Order lock."""
    _aware(at, "at")
    instant = at or timeutils.now()
    row = payment_selectors.get_refund(refund_id)
    if row is None:
        raise payment_services.RefundNotFound(f"Refund {refund_id} does not exist")
    if row.state not in (enums.RefundState.MANUAL_REQUIRED, enums.RefundState.UNKNOWN):
        raise InvalidTransition(MESSAGE_REFUND_NOT_RESOLVABLE)
    with transaction.atomic():
        order = lock_order(row.order_id)
        if outcome == enums.RefundState.SUCCEEDED:
            result = payment_services.mark_succeeded(refund_id, instant, actor_id=actor_id, request=request)
        elif outcome == enums.RefundState.FAILED:
            result = payment_services.mark_failed(
                refund_id, "resolved_failed", at=instant, actor_id=actor_id, request=request
            )
        else:
            raise ValueError(f"A refund resolves to succeeded or failed, not {outcome!r}")
        record_refund_outcome(order, result, at=instant)
        return result


def retry_refund(
    refund_id: int, actor_id: int, *, at: datetime | None = None, request: Any = None
) -> payment_services.RefundResult:
    """Retry a ``failed`` refund (contract §7 item 18 ``…/retry/``): a new refund of the payment (Click →
    ``requested`` + outbox, Payme → ``manual_required``) and its ``refund_requested`` event."""
    _aware(at, "at")
    instant = at or timeutils.now()
    row = payment_selectors.get_refund(refund_id)
    if row is None:
        raise payment_services.RefundNotFound(f"Refund {refund_id} does not exist")
    if row.state != enums.RefundState.FAILED:
        raise InvalidTransition(MESSAGE_REFUND_NOT_RETRYABLE)
    with transaction.atomic():
        order = lock_order(row.order_id)
        if any(r.state in OPEN_REFUND_STATES for r in payment_selectors.refunds_for_order(row.order_id)):
            raise Conflict(MESSAGE_REFUND_OPEN)
        result = payment_services.retry_refund(refund_id, actor_id, at=instant, request=request)
        record_event(
            order,
            _E.REFUND_REQUESTED,
            {
                **_refund_event_data(result),
                "manual": result.state == enums.RefundState.MANUAL_REQUIRED,
            },
            actor_id=actor_id,
            at=instant,
        )
        return result


__all__ = [
    "CancelResult",
    "CheckoutError",
    "CommerceError",
    "OrderNotFound",
    "OutOfStock",
    "apply_bts_status",
    "cancel_order",
    "clean_reason",
    "confirm_payment_effects",
    "create_order",
    "expire_due_reservations",
    "expire_reservation",
    "fail_payment_effects",
    "fake_scenario",
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
