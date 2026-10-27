"""factory-boy factories for the commerce models (call them as ``XFactory.create(...)``).

``OrderFactory`` builds a consistent order: the money columns satisfy ``order_total_formula`` and
``order_amounts_range``, the address columns match ``fulfilment``, and the lifecycle columns match ``status``
through the traits ``paid`` (processing), ``handed`` (handed_to_courier), ``delivered``, ``cancelled`` (admin
cancel) and ``expired`` (reservation expiry). Pass ``items=[{...}, ...]`` (``OrderItemFactory`` kwargs) to
snapshot lines; the default is one line whose ``line_total`` equals ``items_subtotal`` so ``Σ line_total ==
items_subtotal`` always holds, and ``items=[]`` creates none. :func:`order_with_lines` derives the subtotals
from the lines instead.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from factory.declarations import LazyAttribute, LazyFunction, SubFactory, Trait
from factory.django import DjangoModelFactory
from factory.helpers import post_generation

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, OrderItem, StockReservation
from glowderma.core import enums, timeutils

DESIGN_ADDRESS = "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon"


def _now() -> datetime:
    return timeutils.now()


class CartFactory(DjangoModelFactory[Cart]):
    class Meta:
        model = Cart

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]


class CartItemFactory(DjangoModelFactory[CartItem]):
    class Meta:
        model = CartItem

    cart = SubFactory(CartFactory)  # type: ignore[no-untyped-call]
    product = SubFactory(ProductFactory)  # type: ignore[no-untyped-call]
    quantity = 1


class OrderFactory(DjangoModelFactory[Order]):
    class Meta:
        model = Order
        skip_postgeneration_save = True

    class Params:
        paid = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.PROCESSING,
            paid_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=3)),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.paid_at or o.created_at),  # type: ignore[no-untyped-call]
        )
        handed = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.HANDED_TO_COURIER,
            paid_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=3)),  # type: ignore[no-untyped-call]
            handed_to_courier_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=158)),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.handed_to_courier_at or o.created_at),  # type: ignore[no-untyped-call]
        )
        delivered = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.DELIVERED,
            paid_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=3)),  # type: ignore[no-untyped-call]
            delivered_at=LazyAttribute(lambda o: o.created_at + timedelta(hours=26)),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.delivered_at or o.created_at),  # type: ignore[no-untyped-call]
        )
        cancelled = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.CANCELLED,
            cancelled_at=LazyAttribute(lambda o: o.created_at + timedelta(minutes=60)),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.cancelled_at or o.created_at),  # type: ignore[no-untyped-call]
            cancellation_reason=enums.CancellationReason.ADMIN,
            cancellation_note="Mijoz so‘rovi bilan",
            cancelled_by=SubFactory(StaffUserFactory),  # type: ignore[no-untyped-call]
        )
        expired = Trait(  # type: ignore[no-untyped-call]
            status=enums.OrderStatus.CANCELLED,
            cancelled_at=LazyAttribute(lambda o: o.reservation_expires_at),  # type: ignore[no-untyped-call]
            status_changed_at=LazyAttribute(lambda o: o.cancelled_at or o.created_at),  # type: ignore[no-untyped-call]
            cancellation_reason=enums.CancellationReason.RESERVATION_EXPIRED,
        )
        pickup = Trait(  # type: ignore[no-untyped-call]
            fulfilment=enums.FulfilmentMethod.PICKUP,
            address_text="",
            address_region_code="",
            address_city_code="",
            address_comment="",
            delivery_fee=0,
        )

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    channel = enums.OrderChannel.IOS
    fulfilment = enums.FulfilmentMethod.DELIVERY
    address_text = DESIGN_ADDRESS
    address_region_code = "10"
    address_city_code = "1001"
    recipient_name = LazyAttribute(lambda o: o.customer.name)  # type: ignore[no-untyped-call]
    recipient_phone = LazyAttribute(lambda o: o.customer.phone)  # type: ignore[no-untyped-call]
    items_subtotal = 185_000
    promo_subtotal = 0
    delivery_fee = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: 25_000 if o.fulfilment == enums.FulfilmentMethod.DELIVERY else 0
    )
    cashback_used = 0
    total = LazyAttribute(lambda o: o.items_subtotal + o.delivery_fee - o.cashback_used)  # type: ignore[no-untyped-call]
    payment_provider = enums.PaymentProvider.CLICK
    reservation_minutes = 15
    created_at = LazyFunction(_now)  # type: ignore[no-untyped-call]
    status_changed_at = LazyAttribute(lambda o: o.created_at)  # type: ignore[no-untyped-call]
    reservation_expires_at = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: o.created_at + timedelta(minutes=o.reservation_minutes)
    )

    @post_generation  # type: ignore[untyped-decorator]
    def items(obj: Order, create: bool, extracted: Sequence[dict[str, Any]] | None, **kwargs: Any) -> None:
        """One default line worth ``items_subtotal`` (``None``), the given lines, or none (``[]``)."""
        if not create:
            return
        if extracted is None:
            if obj.items_subtotal > 0:
                OrderItemFactory.create(order=obj, quantity=1, unit_price=obj.items_subtotal, promo=False)
            return
        for position, line in enumerate(extracted):
            OrderItemFactory.create(order=obj, position=position, **line)


def order_with_lines(lines: Sequence[tuple[int, int, bool]], **overrides: Any) -> Order:
    """An order whose subtotals are derived from its ``(quantity, unit_price, promo)`` lines."""
    items_subtotal = sum(quantity * unit_price for quantity, unit_price, _ in lines)
    promo_subtotal = sum(quantity * unit_price for quantity, unit_price, promo in lines if promo)
    items = [
        {"quantity": quantity, "unit_price": unit_price, "promo": promo}
        for quantity, unit_price, promo in lines
    ]
    return OrderFactory.create(
        items_subtotal=items_subtotal, promo_subtotal=promo_subtotal, items=items, **overrides
    )


class OrderItemFactory(DjangoModelFactory[OrderItem]):
    class Meta:
        model = OrderItem

    order = SubFactory(OrderFactory, items=[])  # type: ignore[no-untyped-call]
    position = LazyAttribute(lambda o: o.order.items.count())  # type: ignore[no-untyped-call]
    product = SubFactory(ProductFactory)  # type: ignore[no-untyped-call]
    name = LazyAttribute(lambda o: o.product.display_name or o.product.billz_id)  # type: ignore[no-untyped-call]
    billz_id = LazyAttribute(lambda o: o.product.billz_id)  # type: ignore[no-untyped-call]
    quantity = 1
    unit_price = LazyAttribute(lambda o: o.product.price)  # type: ignore[no-untyped-call]
    promo = False
    line_total = LazyAttribute(lambda o: o.quantity * o.unit_price)  # type: ignore[no-untyped-call]


class OrderEventFactory(DjangoModelFactory[OrderEvent]):
    """An ``order_created`` event by default; override type, source, status_after and data together."""

    class Meta:
        model = OrderEvent

    order = SubFactory(OrderFactory)  # type: ignore[no-untyped-call]
    type = enums.OrderEventType.ORDER_CREATED
    source = enums.EventSource.SYSTEM
    status_after = enums.OrderStatus.AWAITING_PAYMENT
    data = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: {"channel": o.order.channel, "item_count": o.order.items.count(), "total": o.order.total}
    )
    at = LazyAttribute(lambda o: o.order.created_at)  # type: ignore[no-untyped-call]


class StockReservationFactory(DjangoModelFactory[StockReservation]):
    class Meta:
        model = StockReservation

    order = SubFactory(OrderFactory)  # type: ignore[no-untyped-call]
    product = SubFactory(ProductFactory)  # type: ignore[no-untyped-call]
    quantity = 1
    state = StockReservation.State.ACTIVE
    expires_at = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: o.order.reservation_expires_at or (o.order.created_at + timedelta(minutes=15))
    )


__all__ = [
    "DESIGN_ADDRESS",
    "CartFactory",
    "CartItemFactory",
    "OrderEventFactory",
    "OrderFactory",
    "OrderItemFactory",
    "StockReservationFactory",
    "order_with_lines",
]
